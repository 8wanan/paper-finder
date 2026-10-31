"""给定的论文没有摘要时，去 arXiv 按标题找一份回来。

为什么需要（实测）：ICSE / FSE / ASE / CCS 这些 ACM 会议在 OpenAlex 里
**大量记录没有摘要**（ACM 不给第三方摘要）。没有摘要的后果是连锁的：
  · 写不出一句话简介 → 用户在日报里只看到标题；
  · L2 语义精排直接跳过（无摘要不猜）；
  · 结果这些顶会论文要么进"需人工确认"桶，要么被日报排除。

而这类论文**大多数有 arXiv 预印本**。所以这里做一件很窄的事：按标题去
arXiv 找同名论文，把摘要（和 arXiv ID）补进账本。合法、便宜、可验证。

匹配用归一化标题的 token Jaccard ≥ 0.8，避免"标题相似但其实是另一篇"。
"""

from __future__ import annotations

import re

from .normalize import clean_text, norm_title

__all__ = ["title_similarity", "backfill_abstracts"]

_STOP = {"a", "an", "the", "of", "for", "and", "on", "in", "to", "with", "via",
         "towards", "toward", "through", "by", "at", "from", "using"}


def _tokens(title: str) -> set:
    words = re.findall(r"[a-z0-9]+", clean_text(title).lower())
    return {w for w in words if w not in _STOP and len(w) > 1}


def title_similarity(left: str, right: str) -> float:
    """标题 token 的 Jaccard 相似度（自己实现，不引入依赖）。"""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _queries_for(title: str) -> list:
    """给一个标题准备两组检索式，按优先级排列。

    实测：只写 `ti:前8个实词` 时，很多顶会论文在 arXiv 上标题被改写过，
    排序前 5 条里根本没有它（相似度 0.1 左右）。加一组 `all:"整句短语"`
    的精确检索，命中率明显更好；两组都试过才判定"找不到"。
    """
    words = [w for w in re.findall(r"[A-Za-z0-9]+", clean_text(title))]
    keep = [w for w in words if w.lower() not in _STOP][:8]
    queries = []
    if keep:
        queries.append("ti:" + " ".join(keep))
    cleaned = clean_text(title).replace('"', "")
    if cleaned:
        queries.append(f'all:"{cleaned}"')
    return queries


def backfill_abstracts(store, arxiv_source, profile, run_id=None, limit=20,
                       threshold=0.8, log=None, ledger_wide=False,
                       ignore_miss_cache=False):
    """给缺摘要的论文补 arXiv 摘要。返回统计。

    ledger_wide=True 时不看 run，直接在整本账本里挑（按已存分数降序）。
    """
    log = log or (lambda msg: None)
    if arxiv_source is None:
        log("  · 未启用 arXiv 源，跳过摘要回填")
        return {"checked": 0, "filled": 0, "missed": 0, "skipped": 0}

    if ledger_wide or run_id is None:
        candidates = store.conn.execute(
            "SELECT pid, title, abstract, l1 FROM papers "
            "WHERE (abstract IS NULL OR TRIM(abstract)='') AND title <> '' "
            "ORDER BY COALESCE(l1,0) DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        candidates = store.conn.execute(
            "SELECT p.pid, p.title, p.abstract, p.l1 FROM papers p "
            "JOIN run_papers rp ON rp.pid=p.pid WHERE rp.run_id=? "
            "AND (p.abstract IS NULL OR TRIM(p.abstract)='') AND p.title <> '' "
            "ORDER BY COALESCE(p.l1,0) DESC LIMIT ?",
            (run_id, limit),
        ).fetchall()

    checked = filled = missed = skipped = 0
    for row in candidates:
        pid, title = row["pid"], row["title"]
        if not ignore_miss_cache and store.get_meta(f"abs_miss:{pid}"):
            skipped += 1
            continue
        checked += 1
        best, best_score = None, 0.0
        for query in _queries_for(title):
            try:
                papers = arxiv_source.fetch(query, limit=5, sort="relevance")
            except Exception as exc:  # noqa: BLE001 - 单篇失败不该打断整轮
                log(f"    ! arXiv 查询失败（{clean_text(title)[:40]}）：{exc}")
                papers = []
            for item in papers:
                score = title_similarity(title, item.get("title"))
                if score > best_score:
                    best, best_score = item, score
            if best_score >= threshold:
                break
        if best is None or best_score < threshold or not best.get("abstract"):
            store.set_meta(f"abs_miss:{pid}", "1")
            missed += 1
            continue

        # 只补空白字段：`upsert` 的合并策略就是这么定义的，不会覆盖已有元数据
        store.upsert(
            {
                "title": clean_text(best.get("title")),
                "abstract": best["abstract"],
                "arxiv_id": best.get("arxiv_id"),
                "year": best.get("year"),
                "sources": ["arxiv"],
                "raw_ids": best.get("raw_ids") or {},
            }
        )
        filled += 1
        log(f"    + 补摘要：{clean_text(title)[:46]}（相似度 {best_score:.2f}）")
    store.commit()
    log(f"  · 摘要回填：检查 {checked}，补齐 {filled}，未找到 {missed}，跳过 {skipped}")
    return {"checked": checked, "filled": filled, "missed": missed, "skipped": skipped}
