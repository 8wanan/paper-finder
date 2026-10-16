"""金标集评测 —— 唯一能证明"筛选变好了还是变坏了"的东西。

没有金标集，references/scoring.md 里的权重调参就是盲调。
这也是把"输出 Top 10"这种不可验证的目标，换成可测量指标的关键一步。

指标（DESIGN.md §8.2）：
    Recall@N     金标正样本中，有多少排进了 L1 前 N
    must_recall  必读核心论文必须全部进前 50，漏一篇就报失败
    Precision@10 前 10 篇里真相关的比例（在已标注的子集上计算）

注意 Precision 的口径：金标集只标注了 25 正 + 25 负，前 10 篇里
可能有"未标注"的论文。所以会同时给出"标注内精确率"与"覆盖数"，
不假装知道未标注论文的答案。
"""

from __future__ import annotations

import json

from .normalize import is_same_paper, norm_arxiv, norm_doi, norm_title
from .yamlish import load

__all__ = ["load_golden", "evaluate", "format_report"]

DEFAULT_TOP_NS = (10, 20, 50, 100)


def _dedupe(values):
    seen, out = set(), []
    for value in values or []:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def load_golden(path: str) -> dict:
    data = load(path) or {}
    positives = [p for p in (data.get("positives") or []) if isinstance(p, dict)]
    negatives = [p for p in (data.get("negatives") or []) if isinstance(p, dict)]
    must = [m for m in (data.get("must_recall") or []) if m]
    return {
        "positives": positives,
        "negatives": negatives,
        "must_recall": must,
        "profile": data.get("profile"),
        "path": path,
    }


def _candidate_keys(entry: dict) -> list:
    keys = []
    arxiv_id = norm_arxiv(entry.get("arxiv_id"))
    if arxiv_id:
        keys.append(("arxiv", arxiv_id))
    doi = norm_doi(entry.get("doi"))
    if doi:
        keys.append(("doi", doi))
    title = norm_title(entry.get("title") or entry.get("name"))
    if title:
        keys.append(("ntitle", title))
    return keys


def _match_pid(store, entry: dict, papers):
    """把金标条目匹配到账本里的 pid。

    先走别名表（精确、快），失败再退回标题模糊匹配 ——
    因为 arXiv 上真的有拼错的标题（DolphinAtack），精确匹配不够用。
    """
    for id_type, id_val in _candidate_keys(entry):
        row = store.conn.execute(
            "SELECT pid FROM aliases WHERE id_type=? AND id_val=?", (id_type, id_val)
        ).fetchone()
        if row:
            return row["pid"]
    target = {
        "title": entry.get("title") or entry.get("name") or "",
        "arxiv_id": entry.get("arxiv_id"),
        "doi": entry.get("doi"),
    }
    for row in papers:
        if is_same_paper(target, {"title": row["title"], "arxiv_id": row["arxiv_id"], "doi": row["doi"]}):
            return row["pid"]
    return None


def evaluate(store, golden_path: str, run_id=None, top_ns=DEFAULT_TOP_NS, profile=None):
    """跑评测，返回结构化结果。

    只对**已打分**的论文排名（scored_only）。原因：SQLite 在 ORDER BY l1 DESC 时
    把 NULL 排最后，未打分的论文会沉底。早期版本没做这个限制，结果一篇没打分的
    相关老论文被算成"第 1158 名"，得出完全错误的召回结论。
    """
    golden = load_golden(golden_path)

    ranked = store.ranked(run_id=run_id, scored_only=True)
    rank_of = {row["pid"]: index + 1 for index, row in enumerate(ranked)}
    papers = list(ranked)

    positives, negatives = [], []
    for entry in golden["positives"]:
        pid = _match_pid(store, entry, papers)
        positives.append(
            {
                "title": entry.get("title") or entry.get("name") or "?",
                "required": bool(entry.get("required")),
                "pid": pid,
                "rank": rank_of.get(pid) if pid else None,
                "matched": pid is not None,
            }
        )
    for entry in golden["negatives"]:
        pid = _match_pid(store, entry, papers)
        negatives.append(
            {
                "title": entry.get("title") or entry.get("name") or "?",
                "pid": pid,
                "rank": rank_of.get(pid) if pid else None,
                "matched": pid is not None,
            }
        )

    total_positives = len(positives) or 1
    recall = {}
    for n in top_ns:
        hit = sum(1 for item in positives if item["rank"] is not None and item["rank"] <= n)
        recall[n] = {
            "hits": hit,
            "total": len(positives),
            "value": round(hit / total_positives, 4),
        }

    # must_recall 条目通常就是 positives 里的论文，但只写了标题。
    # 必须回连到 positives，才能用上 arxiv_id —— 否则 "DolphinAttack" 会因为
    # arXiv 上标题被拼成 "DolphinAtack"（少一个 t）而匹配失败。实测踩到过。
    positive_by_ntitle = {}
    for entry, item in zip(golden["positives"], positives):
        key = norm_title(entry.get("title") or entry.get("name"))
        if key:
            positive_by_ntitle[key] = item

    must = []
    for name in golden["must_recall"]:
        linked = positive_by_ntitle.get(norm_title(name))
        if linked is not None:
            pid, rank = linked["pid"], linked["rank"]
        else:
            pid = _match_pid(store, {"title": name}, papers)
            rank = rank_of.get(pid) if pid else None
        must.append(
            {
                "title": name,
                "matched": pid is not None,
                "rank": rank,
                "ok": rank is not None and rank <= 50,
            }
        )

    # Precision@10：只在已标注子集上计算，并如实报告覆盖
    top_slice = ranked[:10]
    positive_pids = {item["pid"] for item in positives if item["pid"]}
    negative_pids = {item["pid"] for item in negatives if item["pid"]}
    labeled_hits = sum(1 for row in top_slice if row["pid"] in positive_pids)
    labeled_misses = sum(1 for row in top_slice if row["pid"] in negative_pids)
    labeled_total = labeled_hits + labeled_misses
    precision = {
        "top_n": len(top_slice),
        "labeled_hits": labeled_hits,
        "labeled_misses": labeled_misses,
        "labeled_total": labeled_total,
        "precision_on_labeled": round(labeled_hits / labeled_total, 4) if labeled_total else None,
        "precision_over_top": round(labeled_hits / len(top_slice), 4) if top_slice else None,
    }

    missing = _dedupe([item["title"] for item in positives if not item["matched"]])

    # 种子论文是否真的在账本里 —— 不在就说明查询表有洞（实测抓到过 DolphinAttack 缺失）
    seed_check = []
    if profile is not None:
        all_papers = store.conn.execute("SELECT * FROM papers").fetchall()
        for seed in profile.seeds:
            seed_pid = _match_pid(store, seed, papers) or _match_pid(store, seed, all_papers)
            seed_check.append(
                {
                    "title": seed.get("title") or "?",
                    "in_ledger": seed_pid is not None,
                    "rank": rank_of.get(seed_pid) if seed_pid else None,
                }
            )

    return {
        "golden_path": golden_path,
        "run_id": run_id,
        "corpus_size": len(ranked),
        "unscored": store.unscored_count(),
        "positives_total": len(positives),
        "positives_matched": sum(1 for item in positives if item["matched"]),
        "negatives_total": len(negatives),
        "negatives_matched": sum(1 for item in negatives if item["matched"]),
        "recall": recall,
        "must_recall": must,
        "precision": precision,
        "missing_positives": missing,
        "seed_check": seed_check,
        "positives": positives,
        "negatives": negatives,
    }


def format_report(result: dict) -> str:
    lines = []
    lines.append("金标集评测报告")
    lines.append("=" * 52)
    lines.append(f"金标集      : {result['golden_path']}")
    lines.append(f"参与排名    : {result['corpus_size']} 篇已打分论文")
    if result.get("unscored"):
        lines.append(
            f"⚠️  未打分    : {result['unscored']} 篇未参与排名"
            f"（跑 `papers.py rescore` 可覆盖全账本）"
        )
    lines.append(
        f"正样本匹配  : {result['positives_matched']}/{result['positives_total']} 能找到"
    )
    lines.append("")
    lines.append("Recall@N（目标：Recall@50 >= 0.92）")
    for n, item in sorted(result["recall"].items()):
        bar = "█" * int(round(item["value"] * 20))
        lines.append(f"  @{n:<4} {item['value']:.2f}  {item['hits']:>3}/{item['total']:<3} {bar}")
    lines.append("")

    must = result["must_recall"]
    if must:
        failed = [item for item in must if not item["ok"]]
        lines.append(f"必读核心召回: {len(must) - len(failed)}/{len(must)} 通过")
        for item in must:
            mark = "✓" if item["ok"] else "✗"
            rank = item["rank"] if item["rank"] else ("未入库" if not item["matched"] else ">50")
            lines.append(f"  {mark} rank={rank:<8} {item['title'][:56]}")
        lines.append("")

    precision = result["precision"]
    lines.append("Precision@10")
    if precision["precision_on_labeled"] is None:
        lines.append("  前 10 篇中没有已标注论文，无法计算（请补全金标集）")
    else:
        lines.append(
            f"  标注内精确率 {precision['precision_on_labeled']:.2f} "
            f"（命中 {precision['labeled_hits']} / 标注 {precision['labeled_total']}）"
        )
        lines.append(
            f"  前 10 篇中命中正样本 {precision['labeled_hits']}/{precision['top_n']}"
            f"，其余为未标注——不假装知道答案"
        )
    lines.append("")

    if result["missing_positives"]:
        lines.append("❌ 完全没召回到的正样本（说明查询表有洞，优先补这里）：")
        for title in result["missing_positives"]:
            lines.append(f"  - {title[:72]}")
        lines.append("")

    seed_check = result.get("seed_check") or []
    if seed_check:
        missing_seeds = [item for item in seed_check if not item["in_ledger"]]
        lines.append(f"种子论文在账本中: {len(seed_check) - len(missing_seeds)}/{len(seed_check)}")
        for item in missing_seeds:
            lines.append(f"  ✗ 缺失 {item['title'][:64]}")
        if missing_seeds:
            lines.append("  ↑ 种子缺失说明查询表漏了这条脉络，应补充查询或放宽年份下限。")
        lines.append("")

    return "\n".join(lines)


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1)
