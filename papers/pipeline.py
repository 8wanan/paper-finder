"""流水线编排：抓取 → 归一化去重 → 记账 → 分级筛选。

成本分级的落地位置（你提的"粗找 / 细找"）：
    - 抓取与 L0/L1 是**零成本**的，对全部候选执行。
    - L2 只对 L1 头部 top-N 执行（默认 40 篇），且必须命中 llm_cache。
    这样"只有最后几篇才需要精度"不是靠自觉，而是代码写死的预算。
"""

from __future__ import annotations

import json
import sys
import time

from .net import HttpError, RateLimited
from .normalize import clean_text
from .screen import LLMScreener, bucket_of, passes_l0, score_l1
from .store import SUBSTANTIVE_MODES

__all__ = ["run_queries", "rescore", "import_census_file", "seed_sync", "Log"]


class Log:
    """极简日志：写 stderr，不污染 stdout 的机器可读输出。"""

    def __init__(self, verbose: bool = True, stream=None):
        self.verbose = verbose
        self.stream = stream or sys.stderr

    def __call__(self, message: str = ""):
        if self.verbose:
            print(message, file=self.stream, flush=True)


def run_queries(
    store,
    profile,
    sources,
    queries,
    limit: int = 100,
    from_date: str | None = None,
    mode: str = "census",
    min_year: int | None = None,
    sort: str = "relevance",
    log=None,
):
    """执行一批查询，入库并建立运行记录。返回 run_id。"""
    log = log or Log()
    run_id = store.start_run(mode, profile.name, [q["id"] for q in queries])

    papers_by_pid: dict = {}
    query_ids_by_pid: dict = {}
    is_new_by_pid: dict = {}
    l0_rejected = 0
    disabled_sources = set()
    n_fetched = 0

    for query in queries:
        for source in sources:
            if source.name in disabled_sources:
                continue
            log(f"  [{query['id']}] {source.label} ← {query['q'][:70]}")
            try:
                fetched = source.fetch(query["q"], limit=limit, from_date=from_date, sort=sort)
            except RateLimited as exc:
                disabled_sources.add(source.name)
                log(f"    ! {source.label} 配额耗尽，本轮停用该源：{exc}")
                continue
            except HttpError as exc:
                log(f"    ! {source.label} 失败，跳过：{exc}")
                continue
            except Exception as exc:  # 单源异常绝不能拖垮整轮
                log(f"    ! {source.label} 意外异常，跳过：{type(exc).__name__}: {exc}")
                continue

            n_fetched += len(fetched)
            log(f"    · 取回 {len(fetched)} 篇")

            for paper in fetched:
                passed, reason = passes_l0(paper, profile, min_year=min_year)
                pid, is_new = store.upsert(paper)
                if not passed:
                    l0_rejected += 1
                    continue
                papers_by_pid[pid] = True
                is_new_by_pid[pid] = is_new_by_pid.get(pid, False) or is_new
                query_ids_by_pid.setdefault(pid, set()).add(query["id"])

            store.commit()

    # 记录查询命中（S_hit 的数据来源）与运行关联
    for pid, query_ids in query_ids_by_pid.items():
        for query_id in query_ids:
            store.record_paper_query(pid, query_id, run_id)
        store.record_run_paper(run_id, pid, is_new_by_pid.get(pid, False))
    store.commit()

    n_candidates = len(query_ids_by_pid)
    store.finish_run(
        run_id,
        n_fetched=n_fetched,
        n_new=sum(1 for v in is_new_by_pid.values() if v),
        n_strong=n_candidates,
        note=f"L0 剔除 {l0_rejected} 篇；停用源 {sorted(disabled_sources) or '无'}",
    )
    log(
        f"  → 运行 #{run_id}：取回 {n_fetched} 篇，候选 {n_candidates} 篇，"
        f"新增 {sum(1 for v in is_new_by_pid.values() if v)} 篇，"
        f"L0 剔除 {l0_rejected} 篇"
    )
    return run_id


def rescore(store, profile, run_id=None, use_llm: bool = False, llm_top: int = 40,
            env: dict | None = None, log=None, today=None, all_papers: bool = False):
    """重算 L1（并按需跑 L2）。返回统计信息。

    默认对**整个账本**打分，因为：
      · 冷启动导入的 seed-census 论文没有分数，而未打分的论文在 SQLite 里
        `ORDER BY l1 DESC` 会被排到最后 —— 直接导致金标集评测把相关的老论文
        算成"第 1158 名"，得出错误的召回结论。
      · 打分是纯计算，1200 篇约 1 秒；真正贵的是网络抓取（arXiv 限速 3.2 秒/次）。
        所以"改完 profile 重算"应该尽量覆盖全账本。
    传 run_id 则只重算该次运行的论文。
    """
    log = log or Log()
    # 默认取最近一次"有实质内容"的运行（跳过 import / seed-sync 记账），
    # 否则 L2 会把预算花在 seed-sync 那几篇论文上。
    if run_id is not None:
        run = store.get_run(run_id)
    else:
        run = store.latest_run(modes=SUBSTANTIVE_MODES)
    if run is None and not all_papers:
        raise LookupError("账本里还没有任何运行记录")

    if run_id is not None:
        rows = store.run_paper_rows(run_id)
    else:
        rows = store.conn.execute("SELECT * FROM papers").fetchall()

    log(f"  重算 {len(rows)} 篇论文的 L1 分数…")

    scored = 0
    for row in rows:
        query_hits = store.query_count(row["pid"])
        paper = {
            "title": row["title"],
            "abstract": row["abstract"],
            "categories": json.loads(row["categories"] or "[]"),
            "authors": json.loads(row["authors"] or "[]"),
            "sources": json.loads(row["sources"] or "[]"),
            "year": row["year"],
            "published": row["published"],
            "venue": row["venue"],
            "arxiv_id": row["arxiv_id"],
        }
        l1, detail = score_l1(paper, profile, query_hits=query_hits, today=today)
        store.set_l1(row["pid"], l1, detail)
        scored += 1
    store.commit()

    # 让历史运行记录里的分数与 papers 表保持一致
    store.conn.execute(
        "UPDATE run_papers SET l1=(SELECT l1 FROM papers WHERE papers.pid=run_papers.pid)"
    )
    store.commit()

    l2_count = 0
    cache_hits = 0
    if use_llm:
        screener = LLMScreener(store, profile, env or {}, log=log)
        if not screener.available:
            log("  ! 未配置 LLM_API_KEY / LLM_MODEL_CHEAP，跳过 L2（回退到 L1-only）")
        else:
            # 成本闸门：只对 L1 前 llm_top 篇做语义精排
            target_run = run_id if run_id is not None else (run["run_id"] if run else None)
            ranked = store.ranked(run_id=target_run, limit=llm_top, scored_only=True)
            log(f"  L2 精排：仅对 L1 前 {len(ranked)} 篇执行（预算闸门）")
            for index, row in enumerate(ranked, 1):
                result = screener.screen(row)
                if result is None:
                    # 主动跳过（无摘要 / 疑似 prompt injection）：把原因落到 llm_json，
                    # 这样 bucket_of 会把它放进"需人工确认"桶，而不是让它掉进
                    # L1 分桶去冒充一篇高分论文。
                    reason = screener.skipped.get(row["pid"])
                    if reason:
                        store.set_l2(row["pid"], None, {"skipped": reason})
                    continue
                store.set_l2(row["pid"], result["total"], result)
                l2_count += 1
                if index % 10 == 0:
                    store.commit()
            store.conn.execute(
                "UPDATE run_papers SET l2=(SELECT l2 FROM papers WHERE papers.pid=run_papers.pid)"
            )
            store.commit()
            cache_hits = screener.cache_hits
            log(
                f"  L2 完成：打分 {l2_count} 篇，实际调用 {screener.calls} 次，"
                f"缓存命中 {cache_hits} 次"
            )

    if run is not None:
        store.finish_run(
            run["run_id"],
            n_fetched=run["n_fetched"],
            n_new=run["n_new"],
            n_strong=sum(1 for r in store.ranked(run_id=run["run_id"])),
            note=(run["note"] or "") + f" | rescored {scored} | l2 {l2_count}",
        )
    return {"run_id": run["run_id"] if run else None, "scored": scored,
            "l2": l2_count, "cache_hits": cache_hits, "unscored": store.unscored_count()}


def seed_sync(store, profile, sources, log=None):
    """按 ID 精确抓取 profile 里声明的种子论文，补进账本。

    存在的理由（实测发现）：按关键词检索时，2016–2018 的奠基论文会因相关度
    排序靠后而取不回来 —— 实测 DolphinAttack、Hidden Voice Commands 都缺失，
    导致金标集 must_recall 只有 3/8 通过。种子是"必须召回"的集合，
    不能靠关键词碰运气，要按 ID 直接取。
    """
    log = log or Log()
    arxiv_ids = [s.get("arxiv_id") for s in profile.seeds if s.get("arxiv_id")]
    without_id = [s.get("title") for s in profile.seeds if not s.get("arxiv_id")]

    run_id = store.start_run("seed-sync", profile.name, [])
    fetched_total, new_count = 0, 0

    arxiv = next((s for s in sources if s.name == "arxiv"), None)
    if arxiv is None:
        log("  ! arXiv 源未启用，无法按 ID 补齐种子")
    elif arxiv_ids:
        log(f"  按 ID 精确抓取 {len(arxiv_ids)} 篇种子论文…")
        papers = arxiv.fetch_by_ids(arxiv_ids)
        fetched_total = len(papers)
        for paper in papers:
            pid, is_new = store.upsert(paper)
            if is_new:
                new_count += 1
            store.record_run_paper(run_id, pid, is_new)
        store.commit()
        log(f"  · 取回 {fetched_total} 篇，新增 {new_count} 篇")

    if without_id:
        log(f"  · 以下 {len(without_id)} 篇种子没有 arXiv ID，需靠其他源检索：")
        for title in without_id:
            log(f"      - {title}")

    store.finish_run(
        run_id, n_fetched=fetched_total, n_new=new_count, n_strong=new_count,
        note=f"seed-sync：{len(arxiv_ids)} 个 ID，{len(without_id)} 篇无 ID",
    )
    return {"run_id": run_id, "fetched": fetched_total, "new": new_count,
            "without_id": without_id}


def import_census_file(store, path: str, log=None):
    """导入 data/seed-census.json 作为冷启动账本。

    这份数据是实测抓取的 1,196 篇，导入后第一次 brief 就有内容，
    不必等一轮完整的 census 跑完。
    """
    log = log or Log()
    with open(path, "r", encoding="utf-8") as handle:
        records = json.load(handle)

    run_id = store.start_run("import", "seed-census", [])
    new_count = 0
    for record in records:
        arxiv_id = clean_text(record.get("id", "")).rsplit("/", 1)[-1]
        paper = {
            "arxiv_id": arxiv_id or None,
            "title": clean_text(record.get("title")),
            "year": int(record["year"]) if str(record.get("year", "")).isdigit() else None,
            "sources": ["arxiv"],
            "raw_ids": {"arxiv": arxiv_id} if arxiv_id else {},
        }
        pid, is_new = store.upsert(paper)
        if is_new:
            new_count += 1
            store.record_run_paper(run_id, pid, True)
    store.commit()
    store.finish_run(run_id, n_fetched=len(records), n_new=new_count, n_strong=new_count,
                     note="冷启动导入 seed-census.json")
    log(f"  导入 {len(records)} 条，新增 {new_count} 篇（运行 #{run_id}）")
    return run_id, new_count
