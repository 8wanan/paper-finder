"""流水线编排：抓取 → 归一化去重 → 记账 → 分级筛选。

成本分级的落地位置（你提的"粗找 / 细找"）：
    - 抓取与 L0/L1 是**零成本**的，对全部候选执行。
    - L2 只对 L1 头部 top-N 执行（默认 40 篇），且必须命中 llm_cache。
    这样"只有最后几篇才需要精度"不是靠自觉，而是代码写死的预算。
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import time

from .net import HttpError, RateLimited
from .normalize import clean_text, norm_title
from .screen import LLMScreener, bucket_of, passes_l0, score_l1
from .sources import build_toc_source
from .store import SUBSTANTIVE_MODES

__all__ = [
    "run_queries",
    "rescore",
    "import_census_file",
    "seed_sync",
    "collect_conferences",
    "attach_recent_conferences",
    "Log",
]


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
    min_date=None,
    sort: str = "relevance",
    log=None,
):
    """执行一批查询，入库并建立运行记录。返回 run_id。

    min_date 是精确日期下限（watch 模式用），min_year 是年份下限。
    两者分开是因为 `--days 7` 必须按天过滤，不能退化成按年（实测踩过）。
    """
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
                passed, reason = passes_l0(
                    paper, profile, min_year=min_year, min_date=min_date
                )
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


def collect_conferences(store, profile, sources, client, run_id=None, log=None,
                        only_missing=True, force=False):
    """把 profile 里声明的整本会议论文拉进账本。

    与 run_queries 的区别（为什么值得单独写一条路径）：
      · 关键词检索只能拿到"命中查询"的子集，而"顶会全量"要求的是那个会议的
        **完整清单**。用检索去逼近全量，永远差一截且说不清差在哪。
      · 会议清单是一年一两次的批量事件，不是每日增量 —— 混进 watch 里会让
        "今日新增"这个数字失真。

    幂等：采集成功后在 meta 里记 `conf:<venue>:<year>=done`，下次跳过，
    避免每天重复拉 455 篇。`--force` 可强制重采（会议官网补发论文时用）。

    run_id 传了就把论文记进那个运行（daily 用它把「arXiv 增量」和「会议补录」
    合成同一份简报），不传则自建一次 mode=conference 的运行。
    """
    log = log or Log()
    conferences = profile.conferences
    if not conferences:
        log("  ! profile 没有配置 conferences 段，跳过会议采集")
        return {"run_id": run_id, "conferences": 0, "fetched": 0, "new": 0, "rejected": 0}

    own_run = run_id is None
    if own_run:
        run_id = store.start_run("conference", profile.name, [])
        base = {"fetched": 0, "new": 0}
    else:
        row = store.get_run(run_id)
        base = {
            "fetched": int(row["n_fetched"] or 0) if row else 0,
            "new": int(row["n_new"] or 0) if row else 0,
        }

    fetched_total = new_total = rejected = 0
    collected = []
    blocked = {}

    for conf in conferences:
        venue = conf.get("venue")
        year = conf.get("year")
        key = f"conf:{venue}:{year}"
        kind = str(conf.get("kind") or "openalex").strip().lower()
        if only_missing and not force and store.get_meta(key) == "done":
            log(f"  · {venue} {year} 已采集过，跳过（--force 可重采）")
            continue
        if kind in blocked:
            log(f"  · {venue} {year} 跳过：{kind} 源本轮已限流，明天再采（进度已记在账本里）")
            continue

        log(f"  → 采集 {venue} {year}（kind={kind}）")
        try:
            papers = _fetch_conference(conf, sources, client)
        except RateLimited as exc:
            # 关键：一个源被限流不能拖垮整轮。会议采集是**可分次**的，
            # 采到的会议已经写进 meta，明天接着采即可（实测：无 key 的
            # OpenAlex 共享额度会在拉三四本会议后耗尽，Retry-After 可达 20 小时）。
            blocked[kind] = str(exc)
            log(f"    ! 该源已限流，本轮放弃：{exc}")
            continue
        except HttpError as exc:
            # 429 但 Retry-After 很短时，HttpClient 会先自己退避重试；重试完仍 429
            # 就会走到这里。此时**必须把整个源判死**，否则后面每本会议都要再
            # 重试三轮（实测：10 场会议 × 每场 ~90 秒退避 = 卡住 15 分钟）。
            if getattr(exc, "status", None) == 429:
                blocked[kind] = str(exc)
                log(f"    ! 该源返回 429（额度用尽），本轮跳过其余同类会议")
            else:
                log(f"    ! 采集失败，跳过：{exc}")
            continue
        except (LookupError, ValueError) as exc:
            log(f"    ! 采集失败，跳过：{exc}")
            continue

        # 同一会议内按标题去重：官网 TOC 偶尔会把同一篇论文列两次（不同 session）
        seen_titles, unique = set(), []
        for paper in papers:
            fingerprint = norm_title(paper.get("title"))
            if not fingerprint or fingerprint in seen_titles:
                continue
            seen_titles.add(fingerprint)
            unique.append(paper)

        fetched_total += len(unique)
        conf_new = 0
        conf_rejected = 0
        for paper in unique:
            # 先判 L0 再入库：被会议口径/负向词挡掉的论文不该进账本。
            # 账本是资产 —— 混进几千篇明确无关的论文（比如整本 USENIX 里的
            # jailbreak 论文）会污染后续的账本统计与人工核查。
            passed, reason = passes_l0(paper, profile)
            if not passed:
                conf_rejected += 1
                log(f"      · 丢弃「{clean_text(paper.get('title'))[:50]}」：{reason}")
                continue
            pid, is_new = store.upsert(paper)
            store.record_run_paper(run_id, pid, is_new)
            if is_new:
                conf_new += 1
        store.commit()
        new_total += conf_new
        rejected += conf_rejected
        if unique:
            store.set_meta(key, "done")
        log(f"    · 采到 {len(unique)} 篇，新增 {conf_new} 篇，L0 丢弃 {conf_rejected} 篇")
        collected.append({"venue": venue, "year": year, "fetched": len(unique), "new": conf_new})

    store.finish_run(
        run_id,
        n_fetched=base["fetched"] + fetched_total,
        n_new=base["new"] + new_total,
        n_strong=0,
        note=f"会议采集 {len(collected)} 场；L0 丢弃 {rejected} 篇",
    )
    return {
        "run_id": run_id,
        "conferences": len(collected),
        "fetched": fetched_total,
        "new": new_total,
        "rejected": rejected,
        "details": collected,
    }


def _fetch_conference(conf, sources, client):
    """按 kind 分派到对应采集器。"""
    kind = str(conf.get("kind") or "openalex").strip().lower()
    if kind == "openalex":
        openalex = next((s for s in sources if s.name == "openalex"), None)
        if openalex is None:
            raise LookupError("需要先启用 openalex 源（--sources arxiv,openalex）")
        source_id = conf.get("source_id")
        issn = conf.get("issn")
        year = conf.get("year")
        limit = int(conf.get("limit") or 1200)
        if issn:
            # 期刊按 ISSN 过滤（顶刊没有"年鉴"概念）；year 可选
            filters = f"primary_location.source.issn:{issn}"
            if year:
                filters += f",publication_year:{int(year)}"
            return openalex.fetch_by_filters(filters, limit=limit)
        if not source_id:
            raise ValueError("openalex 条目需要 source_id 或 issn")
        return openalex.fetch_venue(source_id, year=year, limit=limit)
    toc = build_toc_source(kind, client)
    if toc is None:
        raise LookupError(f"未知的会议采集器 kind={kind}")
    return toc.fetch(conf)


def attach_recent_conferences(store, profile, run_id, log=None):
    """把账本里"近期顶会论文"挂进本轮运行，好让日报能一起展示。

    为什么需要（用户口径修正）：需求是"只要是顶会，近期的都可以"，
    不是"只要最近 30 天的新论文"。会议一年才发一两次，会议补录发生在
    某一天、之后每天都不会再采 —— 如果日报只渲染"本轮抓到的"，那么
    补录那天之后，顶会论文就再也不会出现在日报里了。

    所以每天出报前，把账本里满足"白名单会议 + 会议窗口内"的论文
    重新挂进当前 run（is_new=0，不影响"新增"计数），让打分层在
    "新预印本 + 近期顶会"这个池子里统一排序。
    """
    log = log or Log()
    from .render import row_date
    from .venues import canonical

    wanted = getattr(profile, "venues", set())
    window = getattr(profile, "conference_max_age_days", None) or 730
    today = _dt.date.today()
    cutoff = today - _dt.timedelta(days=int(window))

    rows = store.conn.execute(
        "SELECT pid, venue, published, year FROM papers WHERE venue IS NOT NULL"
    ).fetchall()
    attached = 0
    for row in rows:
        key = canonical(row["venue"])
        if not key or (wanted and key not in wanted):
            continue
        when = row_date({"published": row["published"], "year": row["year"]})
        if when is not None and when < cutoff:
            continue
        store.record_run_paper(run_id, row["pid"], False)
        attached += 1
    store.commit()
    if attached:
        log(f"  · 近期顶会论文池：{attached} 篇并入本轮（窗口 {int(window)} 天，is_new=0）")
    return attached


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
