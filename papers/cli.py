"""paper-finder CLI —— 唯一入口。

用法示例：
    papers.py doctor
    papers.py import-census
    papers.py census --llm
    papers.py watch --days 14
    papers.py search "audio jailbreak" --since 2024-01-01 --top 20
    papers.py brief
    papers.py decide briefs/brief-12.md
    papers.py eval --golden tests/golden.yaml
    papers.py show 2405.19103

设计原则（DESIGN.md 漏洞 7）：**一个 CLI + 子命令 + JSON 输出**，
而不是每步一个脚本。多脚本会让 agent 反复往返、状态靠自然语言传递，慢且易崩。

为什么 CLI 放在包里而不是仓库根作为 papers.py：脚本 `papers.py` 与包 `papers/`
同名时，`import papers` 的解析结果依赖解释器的查找顺序（CPython 会优先命中目录包），
外部代码想复用 CLI 就会拿到包而不是脚本。放在 `papers/cli.py` 后，
`python papers.py` 与 `from papers.cli import main` 两条路径都确定无歧义。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

# 仓库根目录 = 本文件所在包的上一层
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from . import __version__  # noqa: E402
from .config import DEFAULT_PROFILE, load_profile  # noqa: E402
from .decide import apply_decisions, apply_explicit, load_sidecar, parse_brief  # noqa: E402
from .env import load_env, mask_secret  # noqa: E402
from .evaluate import evaluate, format_report  # noqa: E402
from .net import HttpClient, HttpError, RateLimited  # noqa: E402
from .pipeline import (  # noqa: E402
    Log,
    collect_conferences,
    import_census_file,
    rescore,
    run_queries,
    seed_sync,
)
from .render import render_brief, short_id_for, write_sidecar  # noqa: E402
from .sources import build_sources  # noqa: E402
from .store import SUBSTANTIVE_MODES, Store  # noqa: E402

DEFAULT_GOLDEN = os.path.join("tests", "golden.yaml")
GOLDEN_FALLBACK = os.path.join("tests", "golden.example.yaml")


# ── 公共辅助 ────────────────────────────────────────────────────────
def resolve_db(env: dict) -> str:
    path = env.get("PAPER_FINDER_DB") or os.path.join("data", "papers.db")
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def resolve_golden(explicit: str | None) -> str | None:
    candidates = [explicit] if explicit else [DEFAULT_GOLDEN, GOLDEN_FALLBACK]
    for candidate in candidates:
        path = candidate if os.path.isabs(candidate) else os.path.join(ROOT, candidate)
        if os.path.exists(path):
            return path
    return None


def make_client(env: dict, log) -> HttpClient:
    return HttpClient(
        contact_email=(env.get("ARXIV_CONTACT_EMAIL") or env.get("UNPAYWALL_EMAIL") or "").strip(),
        log=log,
    )


def parse_since(value: str | None):
    """把 --since 解析成 (date_str, year)。支持 YYYY 与 YYYY-MM-DD。"""
    if not value:
        return None, None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            parsed = _dt.datetime.strptime(value, fmt).date()
            return parsed.isoformat(), parsed.year
        except ValueError:
            continue
    raise SystemExit(f"--since 格式无法识别：{value!r}（应为 YYYY 或 YYYY-MM-DD）")


def emit(payload, as_json: bool, human: str | None = None):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    elif human is not None:
        print(human)


def row_to_paper(row) -> dict:
    return {
        "pid": row["pid"],
        "title": row["title"],
        "abstract": row["abstract"],
        "categories": json.loads(row["categories"] or "[]"),
        "authors": json.loads(row["authors"] or "[]"),
        "sources": json.loads(row["sources"] or "[]"),
        "year": row["year"],
        "published": row["published"],
        "venue": row["venue"],
        "arxiv_id": row["arxiv_id"],
        "doi": row["doi"],
    }


# ── 子命令 ──────────────────────────────────────────────────────────
def cmd_doctor(args, env) -> int:
    log = Log(not args.json)
    report = {"ok": True, "checks": []}

    def check(name, ok, detail, fatal=False):
        report["checks"].append({"name": name, "ok": ok, "detail": detail, "fatal": fatal})
        if not ok and fatal:
            report["ok"] = False
        log(f"  {'✓' if ok else '✗'} {name}: {detail}")

    log("paper-finder doctor")
    log("")

    check("python", True, sys.version.split()[0])

    try:
        profile = load_profile(args.profile, ROOT)
        check(
            "profile",
            True,
            f"{profile.name}（{profile.display_name}）"
            f"｜census {len(profile.census_queries)} 条 / search {len(profile.search_queries)} 条"
            f"｜种子 {len(profile.seeds)} 篇",
        )
        # 顶会口径自检：策略写了但白名单是空的时候，venue 过滤会静默失效 ——
        # 这种"看起来配了其实没配"的情况必须报警，否则用户以为在收顶会。
        policy = profile.venue_policy
        venues_count = len(profile.venues)
        confs = profile.conferences
        check(
            "venue-policy",
            not (policy != "off" and venues_count == 0),
            f"策略 {policy}｜白名单 {venues_count} 个会议｜待采会议 {len(confs)} 场",
        )
        if confs:
            missing = [c for c in confs if (c.get("kind") in (None, "openalex") and not c.get("source_id"))]
            missing += [c for c in confs if c.get("kind") in ("usenix", "acl") and not c.get("url")]
            check(
                "conferences",
                not missing,
                f"{len(confs)} 场" + (f"；{len(missing)} 场缺 source_id/url" if missing else ""),
            )
    except Exception as exc:
        check("profile", False, str(exc), fatal=True)

    db_path = resolve_db(env)
    try:
        store = Store(db_path)
        stats = store.stats()
        check(
            "ledger",
            True,
            f"{db_path}｜论文 {stats['papers']} 篇（有摘要 {stats['with_abstract']}）"
            f"｜别名 {stats['aliases']}｜判定 {stats['decisions']}",
        )
        store.close()
    except Exception as exc:
        check("ledger", False, str(exc), fatal=True)

    # OpenAlex 的 key 不是"必要条件"：无 key 只能浅翻、可能 429，
    # 但 2026-10-30 实测无 key 可用。所以这里报状态而不是报失败。
    openalex_key = (env.get("OPENALEX_API_KEY") or "").strip()
    check(
        "openalex key",
        True,
        mask_secret(openalex_key) if openalex_key else "未设置（无 key 走浅翻，429 时自动降级）",
    )
    check("llm key", bool((env.get("LLM_API_KEY") or "").strip()),
          mask_secret(env.get("LLM_API_KEY")))

    # 用真实 log（不是静默的），这样 CA 自动修正这类重要信息用户能看到
    client = make_client(env, log)
    if client.ca_bundle:
        check(
            "ssl-ca",
            True,
            f"当前 Python 缺少默认 CA 证书，已自动改用 {client.ca_bundle}"
            f"（建议设 SSL_CERT_FILE 永久生效）",
        )
    else:
        check("ssl-ca", True, "默认 CA 证书可用")

    sources, skipped = build_sources(
        env, client, names=args.sources.split(",") if args.sources else None
    )
    check("sources", len(sources) > 0, "启用 " + ", ".join(s.name for s in sources))
    for name, reason in skipped:
        check(f"source:{name}", False, f"已跳过 —— {reason}")

    if not args.offline:
        arxiv = next((s for s in sources if s.name == "arxiv"), None)
        if arxiv is None:
            check("arxiv-reachability", False, "arXiv 源未启用", fatal=True)
        else:
            try:
                total = arxiv.count('all:"jailbreak"')
                check("arxiv-reachability", True, f"可达，探测查询命中 {total} 条")
            except RateLimited as exc:
                check("arxiv-reachability", False, str(exc))
            except HttpError as exc:
                check("arxiv-reachability", False, f"不可达：{exc}", fatal=True)
    else:
        check("arxiv-reachability", True, "已跳过（--offline）")

    log("")
    log("结论：" + ("环境就绪" if report["ok"] else "存在致命问题，见上面的 ✗"))
    emit(report, args.json, None)
    return 0 if report["ok"] else 1


def cmd_seed_sync(args, env) -> int:
    log = Log(not args.json)
    profile = load_profile(args.profile, ROOT)
    client = make_client(env, log)
    sources, skipped = build_sources(env, client, names=["arxiv"], log=log)
    for name, reason in skipped:
        log(f"  · 跳过数据源 {name}：{reason}")

    with Store(resolve_db(env)) as store:
        result = seed_sync(store, profile, sources, log=log)
        # 新补进来的种子论文需要立刻打分，否则它们在排名里是 NULL、会沉底
        rescore(store, profile, env=env, log=log)
        stats = store.stats()

    emit({"seed": result, "ledger": stats["papers"]}, args.json,
         f"种子同步完成：新增 {result['new']} 篇"
         + (f"；{len(result['without_id'])} 篇无 arXiv ID 需另行检索"
            if result["without_id"] else ""))


def cmd_dump(args, env) -> int:
    """导出候选论文（含摘要），供 agent 写简介或人工检查。

    这是「让 agent 当 LLM」工作流的第一步：dump → agent 读摘要写简介 →
    annotate 写回 → brief 显示「简介」栏。全程不需要 API key。
    """
    from .render import ranked_rows_for_run

    profile = load_profile(args.profile, ROOT)
    with Store(resolve_db(env)) as store:
        run = store.get_run(args.run) if args.run else store.latest_run(modes=SUBSTANTIVE_MODES)
        if run is None:
            raise SystemExit("没有可导出的运行记录，先跑 census / watch / search")
        rows, rank_of = ranked_rows_for_run(store, run["run_id"], profile)
        existing = store.annotations()

        items = []
        for row in rows:
            rank = rank_of.get(row["pid"])
            if rank is None or rank > args.top:
                continue
            if args.only_missing and row["pid"] in existing:
                continue
            items.append(
                {
                    "pid": row["pid"],
                    "short_id": short_id_for(row["pid"]),
                    "rank": rank,
                    "title": row["title"],
                    "year": row["year"],
                    "arxiv_id": row["arxiv_id"],
                    "url": row["url"] or row["oa_url"],
                    "l1": row["l1"],
                    "abstract": row["abstract"],
                    "has_summary": row["pid"] in existing,
                }
            )

    if args.json:
        emit(
            {"run_id": run["run_id"], "profile": profile.name, "count": len(items),
             "papers": items},
            True,
        )
        return 0

    print(f"运行 #{run['run_id']} · 前 {args.top} 名中导出 {len(items)} 篇")
    print("=" * 78)
    for item in items:
        abstract = " ".join((item["abstract"] or "").split())
        print(f"\n[{item['rank']}] {item['title']}")
        print(f"    pid={item['pid']}  year={item['year']}  L1={item['l1']}")
        if not abstract:
            print("    (无摘要 —— 不要给它写简介，交人工判断)")
        else:
            print(f"    摘要：{abstract[:args.chars]}")
    return 0


def cmd_annotate(args, env) -> int:
    """把简介写进账本。输入 JSONL，每行一个对象。

        {"pid": "arxiv:2405.06134", "summary": "一句话说清在做什么",
         "why_relevant": "可选：与你的方向的关系",
         "quote": "可选：摘要原文片段"}

    quote 会被校验必须逐字出现在摘要里 —— 与 L2 同一道防幻觉闸门。
    """
    from .normalize import clean_text

    if args.file == "-":
        text = sys.stdin.read()
    else:
        path = args.file if os.path.isabs(args.file) else os.path.join(ROOT, args.file)
        if not os.path.exists(path):
            raise SystemExit(f"找不到标注文件：{path}")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()

    entries = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"第 {line_no} 行不是合法 JSON：{exc}")
        if not isinstance(payload, dict) or not payload.get("summary"):
            raise SystemExit(f"第 {line_no} 行缺少 summary 字段")
        entries.append((line_no, payload))

    if not entries:
        raise SystemExit("没有可写入的标注（文件为空或全是注释）")

    applied, rejected = [], []
    with Store(resolve_db(env)) as store:
        for line_no, payload in entries:
            token = payload.get("pid") or payload.get("short_id")
            pid = store.resolve_pid(token) if token else None
            if pid is None:
                rejected.append((line_no, token, "账本里找不到这篇论文"))
                continue

            row = store.get_paper(pid)
            abstract = clean_text(row["abstract"]) if row else ""
            quote = clean_text(payload.get("quote"))

            # 防幻觉闸门：与 L2 一致。有 quote 就必须真在摘要里，
            # 否则这句话是编的，而用户正是靠它决定读不读。
            if quote:
                if not abstract:
                    rejected.append((line_no, token, "该论文没有摘要，无法校验 quote"))
                    continue
                if quote.lower() not in abstract.lower():
                    rejected.append(
                        (line_no, token, f"quote 不逐字出现在摘要里，疑似编造：{quote[:40]}")
                    )
                    continue

            store.set_annotation(
                pid,
                summary=payload["summary"],
                why_relevant=payload.get("why_relevant"),
                quote=quote or None,
                source=payload.get("source") or args.source,
                model=payload.get("model"),
            )
            applied.append((token, pid, payload["summary"]))
        stats = store.stats()

    if args.json:
        emit(
            {"applied": len(applied), "rejected": rejected,
             "annotated_total": stats["annotated"]},
            True,
        )
    else:
        print(f"写入 {len(applied)} 条简介（账本累计 {stats['annotated']} 条）")
        for token, _pid, summary in applied[:5]:
            print(f"  · {token}: {summary[:60]}")
        if len(applied) > 5:
            print(f"  · …另有 {len(applied) - 5} 条")
        if rejected:
            print(f"\n拒绝 {len(rejected)} 条：")
            for line_no, token, reason in rejected:
                print(f"  ✗ 第 {line_no} 行 {token}: {reason}")
    return 0


def cmd_import(args, env) -> int:
    log = Log(not args.json)
    path = args.file if os.path.isabs(args.file) else os.path.join(ROOT, args.file)
    if not os.path.exists(path):
        raise SystemExit(f"找不到冷启动文件：{path}")
    with Store(resolve_db(env)) as store:
        run_id, new_count = import_census_file(store, path, log=log)
        stats = store.stats()
    emit({"run_id": run_id, "new": new_count, "ledger": stats["papers"]}, args.json,
         f"导入完成：运行 #{run_id}，新增 {new_count} 篇，账本累计 {stats['papers']} 篇")


def _select_queries(profile, args, mode: str):
    if mode == "search":
        return [{"id": "S1", "q": args.query, "expect": None, "tier": "search"}]
    if mode == "watch":
        return profile.watch_queries
    if args.tier == "census":
        return profile.census_queries
    if args.tier == "search":
        return profile.search_queries
    return profile.all_queries(
        include_generation=not args.no_generation,
        include_category=not args.no_category,
    )


def _prepare(args, env, log, extra_sources=None):
    """加载 profile / 数据源 / 客户端。三个命令共用，避免参数处理分叉。

    extra_sources：命令需要、但 profile 没配在**检索源**里的数据源。
    目前只有 collect/daily 用 openalex —— 它不参与关键词检索
    （OpenAlex 的 search 是自由文本，喂 arXiv 的布尔语法没有意义），
    只在"按会议拉年鉴"时使用，所以不能混进 profile.sources 的检索源里。
    """
    profile = load_profile(args.profile, ROOT)
    client = make_client(env, log)
    if getattr(args, "sources", None):
        source_names = args.sources.split(",")
    else:
        source_names = list(profile.source_names or [])
        for extra in extra_sources or []:
            if extra not in source_names:
                source_names.append(extra)
        source_names = source_names or None
    sources, skipped = build_sources(env, client, names=source_names, log=log, profile=profile)
    if not sources:
        raise SystemExit("没有任何可用数据源")
    for name, reason in skipped:
        log(f"  · 跳过数据源 {name}：{reason}")
    return profile, sources, client


def _as_date(value):
    if value and len(value) == 10:
        try:
            return _dt.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _harvest(args, env, mode, profile, sources, store, log):
    """跑一轮查询并把候选写进账本，返回 run_id（不渲染简报）。"""
    from_date, since_year = parse_since(getattr(args, "since", None))
    min_date = _as_date(from_date)

    if mode == "watch":
        cutoff = _dt.date.today() - _dt.timedelta(days=int(args.days))
        from_date = cutoff.isoformat()
        since_year = cutoff.year
        min_date = cutoff
        log(f"watch 模式：只看 {cutoff.isoformat()} 之后发表的论文（最近 {args.days} 天）")

    if getattr(args, "min_year", None) is not None:
        since_year = int(args.min_year)

    queries = _select_queries(profile, args, mode)
    if not queries:
        raise SystemExit("没有可执行的查询，检查 profile 配置")

    log(f"模式={mode} profile={profile.name} 查询 {len(queries)} 条 "
        f"数据源 {[s.name for s in sources]}")
    log("")
    return run_queries(
        store, profile, sources, queries,
        limit=int(getattr(args, "limit", 100)),
        from_date=from_date,
        mode=mode,
        min_year=since_year,
        min_date=min_date,
        sort="date" if mode == "watch" else "relevance",
        log=log,
    )


def _render_and_write(store, profile, run_id, args, env, log, out_path=None):
    """打分 + 生成简报 + 落盘。返回 (out_path, brief_stats, markdown, id_map)。"""
    # 不给 run_id：给整个账本打分。打分是纯计算（4500 篇约 3 秒），
    # 而"未打分的论文在排序里沉底"会让评测与简报都失真。
    score_stats = rescore(
        store, profile,
        use_llm=bool(getattr(args, "llm", False)),
        llm_top=int(getattr(args, "llm_top", 40)),
        env=env, log=log,
    )
    has_llm = score_stats["l2"] > 0
    markdown, id_map, brief_stats = render_brief(
        store, profile, run_id=run_id, has_llm=has_llm,
        max_per_bucket=(int(args.max_per_bucket) or None),
        include_maybe=not getattr(args, "hide_maybe", False),
    )
    if getattr(args, "stdout", False):
        return None, brief_stats, markdown, id_map

    if out_path is None:
        briefs_dir = os.path.join(ROOT, "briefs")
        os.makedirs(briefs_dir, exist_ok=True)
        out_path = os.path.join(briefs_dir, f"brief-{run_id}.md")
    elif not os.path.isabs(out_path):
        out_path = os.path.join(ROOT, out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(markdown)
    write_sidecar(out_path, id_map)
    return out_path, brief_stats, markdown, id_map


def _run_and_screen(args, env, mode: str) -> int:
    log = Log(not args.json)
    profile, sources, _client = _prepare(args, env, log)

    with Store(resolve_db(env)) as store:
        run_id = _harvest(args, env, mode, profile, sources, store, log)
        out_path, brief_stats, markdown, id_map = _render_and_write(store, profile, run_id, args, env, log)

    if out_path:
        log("")
        log(f"简报已写入：{out_path}")

    if args.json:
        emit({"run_id": run_id, **brief_stats, "brief": out_path}, True)
    elif out_path is None:
        print(markdown)
    else:
        print(f"运行 #{run_id}｜候选 {brief_stats['candidates']} 篇｜新增 {brief_stats['new']} 篇")
        for key, value in brief_stats["buckets"].items():
            print(f"  {key:<9} {value}")
        print(f"\n打开简报查看：{out_path}")
    return 0


def cmd_collect(args, env) -> int:
    """整本会议采集：把 profile.conferences 里声明的会议一次性拉进账本。

    与 census 的区别：census 是"按关键词把你的领域读完"，collect 是
    "按会议把某一年的正刊清单读完"。要做"顶会日报"，两者都需要。
    """
    log = Log(not args.json)
    profile, sources, client = _prepare(args, env, log, extra_sources=["openalex"])

    with Store(resolve_db(env)) as store:
        result = collect_conferences(
            store, profile, sources, client,
            only_missing=not args.force, log=log,
        )
        run_id = result["run_id"]
        if run_id is None:
            raise SystemExit("没有可采集的会议，检查 profile 的 conferences 段")
        if result["fetched"] == 0:
            log("  · 本次没有采到新论文（可能都已采集过，用 --force 重采）")
        out_path, brief_stats, markdown, _id_map = _render_and_write(
            store, profile, run_id, args, env, log, out_path=args.out
        )

    if args.json:
        emit({"collect": result, **brief_stats, "brief": out_path}, True)
    else:
        print(f"会议采集：{result['conferences']} 场，取回 {result['fetched']} 篇，"
              f"新增 {result['new']} 篇，L0 丢弃 {result['rejected']} 篇")
        for item in result["details"]:
            print(f"  · {item['venue']} {item['year']}: {item['fetched']} 篇（新增 {item['new']}）")
        print(f"\n简报：{out_path}")
    return 0


def cmd_daily(args, env) -> int:
    """每日采集：arXiv 增量 + 到期的会议补录，合成一份带日期的日报。

    产品定义说明（不是实现细节）：会议是脉冲式发布（一年一两次），
    "每天的顶会论文"在会议空窗期其实只有预印本。所以这里有两条输入：
      1. watch（arXiv，按天）；
      2. collect（会议，按"没采过就采"的幂等状态）。
    简报里用「会议」列区分它们 —— 预印本显示"预印本"，已中会显示会议名。
    """
    log = Log(not args.json)
    profile, sources, client = _prepare(args, env, log, extra_sources=["openalex"])

    digest_dir = (env.get("PAPER_FINDER_DIGEST_DIR") or "").strip()
    stamp = _dt.date.today().isoformat()
    # 文件名带 profile：两个方向同一天都要出日报时，不能互相覆盖（实测踩到过）。
    out_path = args.out or os.path.join(
        ROOT, "briefs", f"daily-{profile.name}-{stamp}.md"
    )

    with Store(resolve_db(env)) as store:
        run_id = _harvest(args, env, "watch", profile, sources, store, log)

        conf_result = {"conferences": 0, "fetched": 0, "new": 0, "rejected": 0, "details": []}
        if profile.conferences and not args.no_conferences:
            log("")
            log("会议补录（幂等：已采过的自动跳过）")
            conf_result = collect_conferences(
                store, profile, sources, client, run_id=run_id,
                only_missing=not args.force, log=log,
            )

        out_path, brief_stats, markdown, id_map = _render_and_write(
            store, profile, run_id, args, env, log, out_path=out_path
        )
        store.set_meta("daily:last", f"{stamp} run#{run_id}")

        # 稳定文件名：`latest-<profile>.md` 永远指向最近一份，用户不用记日期。
        # 同时写 sidecar，否则对 latest 副本执行 `decide` 会解析不到短 id。
        if out_path:
            latest_copy = os.path.join(
                os.path.dirname(out_path), f"latest-{profile.name}.md"
            )
            try:
                with open(latest_copy, "w", encoding="utf-8") as handle:
                    handle.write(markdown)
                write_sidecar(latest_copy, id_map)
            except OSError as exc:
                log(f"  ! 写 latest 副本失败（不影响主流程）：{exc}")

    # 可选：把日报再拷一份到指定目录（比如被同步盘同步出去）
    if digest_dir and out_path:
        try:
            target_dir = digest_dir if os.path.isabs(digest_dir) else os.path.join(ROOT, digest_dir)
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, os.path.basename(out_path))
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(markdown)
            log(f"日报副本：{target}")
        except OSError as exc:
            log(f"  ! 拷贝日报失败（不影响主流程）：{exc}")

    if args.json:
        emit({"run_id": run_id, **brief_stats, "collect": conf_result, "brief": out_path}, True)
    else:
        print(f"运行 #{run_id}｜候选 {brief_stats['candidates']} 篇｜新增 {brief_stats['new']} 篇"
              f"｜会议补录 {conf_result['new']} 篇")
        for key, value in brief_stats["buckets"].items():
            print(f"  {key:<9} {value}")
        if brief_stats["candidates"] == 0:
            print("\n今天这个查询集没有命中新论文（窄领域常见）。")
            print(f"想回看更长时间：python papers.py daily --days 7")
        print(f"\n日报：{out_path}")
    return 0


def cmd_census(args, env) -> int:
    return _run_and_screen(args, env, "census")


def cmd_search(args, env) -> int:
    return _run_and_screen(args, env, "search")


def cmd_watch(args, env) -> int:
    return _run_and_screen(args, env, "watch")


def cmd_rescore(args, env) -> int:
    log = Log(not args.json)
    profile = load_profile(args.profile, ROOT)
    with Store(resolve_db(env)) as store:
        result = rescore(
            store, profile, run_id=args.run,
            use_llm=bool(args.llm), llm_top=int(args.llm_top), env=env, log=log,
        )
    scope = f"运行 #{result['run_id']}" if result["run_id"] and args.run else "整个账本"
    emit(result, args.json,
         f"重算完成：{scope}，L1 {result['scored']} 篇，L2 {result['l2']} 篇"
         f"（缓存命中 {result['cache_hits']}）｜剩余未打分 {result['unscored']} 篇")


def cmd_brief(args, env) -> int:
    profile = load_profile(args.profile, ROOT)
    with Store(resolve_db(env)) as store:
        run = store.get_run(args.run) if args.run else store.latest_run(modes=SUBSTANTIVE_MODES)
        if run is None:
            existing = store.stats()["papers"]
            if existing:
                lead = (
                    f"账本里已有 {existing} 篇论文，但还没跑过检索类运行"
                    f"（import / seed-sync 只是记账，不产生简报）。\n"
                    f"跑一条即可：\n"
                )
            else:
                lead = "账本是空的。先跑其中一条：\n"
            raise SystemExit(
                lead
                + "  papers.py census          全量普查你的领域（推荐第一次用这个）\n"
                + "  papers.py watch --days 7  只看最近新增\n"
                + "  papers.py search \"<关键词>\"  临时查一个具体问题"
            )
        has_llm = bool(
            store.conn.execute(
                "SELECT 1 FROM run_papers WHERE run_id=? AND l2 IS NOT NULL LIMIT 1",
                (run["run_id"],),
            ).fetchone()
        )
        markdown, id_map, stats = render_brief(
            store, profile, run_id=run["run_id"], has_llm=has_llm,
            max_per_bucket=(int(args.max_per_bucket) or None),
            archived_preview=int(args.archived_preview),
        )
        out_path = args.out
        if out_path is None:
            briefs_dir = os.path.join(ROOT, "briefs")
            os.makedirs(briefs_dir, exist_ok=True)
            out_path = os.path.join(briefs_dir, f"brief-{run['run_id']}.md")
        elif not os.path.isabs(out_path):
            out_path = os.path.join(ROOT, out_path)
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
        write_sidecar(out_path, id_map)

    if args.json:
        emit({"brief": out_path, **stats}, True)
    else:
        print(f"运行 #{stats['run_id']}｜候选 {stats['candidates']}｜新增 {stats['new']}"
              f"｜模式 {stats['mode']}")
        for key, value in stats["buckets"].items():
            print(f"  {key:<9} {value}")
        print(f"\n简报：{out_path}")
    return 0


def _digest_files(profile_name: str | None = None) -> list:
    """列出日报文件，按修改时间倒序。"""
    briefs_dir = os.path.join(ROOT, "briefs")
    if not os.path.isdir(briefs_dir):
        return []
    prefix = f"daily-{profile_name}-" if profile_name else "daily-"
    files = [
        os.path.join(briefs_dir, name)
        for name in os.listdir(briefs_dir)
        if name.startswith(prefix) and name.endswith(".md")
    ]
    return sorted(files, key=os.path.getmtime, reverse=True)


def _digest_headline(path: str) -> str:
    """从日报里抠出"本轮候选 N 篇 · 新增 M 篇"这行，用作一句话摘要。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for _ in range(12):
                line = handle.readline()
                if not line:
                    break
                if line.startswith("本轮候选"):
                    return line.strip()
    except OSError:
        pass
    return ""


def cmd_latest(args, env) -> int:
    """打印（并可打开）最近一份日报 —— 这是"每天去哪儿看"的固定入口。

    为什么需要它：日报文件名带日期，用户每天要去记"今天是哪一份"。
    `latest` 永远指向最新那份，配合 `--open` 直接调起系统默认应用。
    """
    profile_names = [args.profile] if args.profile else ["ai-vuln-discovery", "ai-methodology"]
    found = []
    for name in profile_names:
        files = _digest_files(name)
        if not files:
            continue
        path = files[0]
        found.append(
            {
                "profile": name,
                "path": path,
                "modified": _dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "headline": _digest_headline(path),
            }
        )

    if not found:
        raise SystemExit(
            "还没有任何日报。先跑一次：\n"
            "  python papers.py daily --profile ai-vuln-discovery\n"
            "（定时任务每天 09:00 会自动跑）"
        )

    if args.open:
        for item in found:
            try:
                os.startfile(item["path"])  # noqa: S606 - Windows 上打开默认应用
            except AttributeError:  # 非 Windows
                import subprocess

                subprocess.run(["open" if os.uname().sysname == "Darwin" else "xdg-open",
                                item["path"]], check=False)

    if args.json:
        emit({"digests": found}, True)
    else:
        for item in found:
            print(f"[{item['profile']}] {item['modified']}")
            print(f"  {item['path']}")
            if item["headline"]:
                print(f"  {item['headline']}")
        print("\n填完最后一列的 k/m/d 后回写：")
        print(f"  python papers.py decide {found[0]['path']}")
    return 0


def cmd_decide(args, env) -> int:
    log = Log(not args.json)
    with Store(resolve_db(env)) as store:
        if args.pid:
            letter = args.decision or "k"
            try:
                pid = apply_explicit(store, args.pid, letter, reason=args.reason)
            except (ValueError, LookupError) as exc:
                raise SystemExit(str(exc))
            emit({"pid": pid, "decision": letter}, args.json, f"已记录：{pid} -> {letter}")
            return 0

        if not args.brief:
            raise SystemExit("请给出简报文件路径，或用 --pid 显式指定")
        path = args.brief if os.path.isabs(args.brief) else os.path.join(ROOT, args.brief)
        if not os.path.exists(path):
            raise SystemExit(f"找不到简报：{path}")
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()

        id_map = load_sidecar(path)
        entries = parse_brief(text)
        if not entries:
            emit({"applied": 0, "unresolved": [], "note": "简报里没有填写任何决策"}, args.json,
                 "简报里还没有任何已填写的决策。把最后一列的 [ ] 改成 [k]/[m]/[d] 再运行。")
            return 0
        applied, unresolved = apply_decisions(store, entries, id_map=id_map, log=log)
        stats = store.stats()

    human = f"回写 {len(applied)} 条判定（账本累计 {stats['decisions']} 条）"
    if unresolved:
        human += f"\n未解析 {len(unresolved)} 条：{', '.join(unresolved[:8])}"
    emit({"applied": len(applied), "unresolved": unresolved,
          "total_decisions": stats["decisions"]}, args.json, human)
    return 0


def cmd_show(args, env) -> int:
    with Store(resolve_db(env)) as store:
        pid = store.resolve_pid(args.id)
        if pid is None:
            raise SystemExit(f"找不到论文：{args.id}")
        row = store.get_paper(pid)
        decision = store.conn.execute("SELECT * FROM decisions WHERE pid=?", (pid,)).fetchone()
        detail = json.loads(row["l1_detail"]) if row["l1_detail"] else None
        payload = {
            "pid": row["pid"], "title": row["title"], "year": row["year"],
            "venue": row["venue"], "authors": json.loads(row["authors"] or "[]"),
            "categories": json.loads(row["categories"] or "[]"),
            "sources": json.loads(row["sources"] or "[]"),
            "arxiv_id": row["arxiv_id"], "doi": row["doi"],
            "url": row["url"], "oa_url": row["oa_url"],
            "citation_count": row["citation_count"], "first_seen": row["first_seen"],
            "l1": row["l1"], "l1_detail": detail, "l2": row["l2"],
            "abstract": row["abstract"],
            "decision": dict(decision) if decision else None,
        }
    if args.json:
        emit(payload, True)
        return 0
    print(f"{row['title']}")
    print("=" * 60)
    print(f"pid        : {payload['pid']}")
    print(f"年份/venue : {payload['year']} / {payload['venue'] or '—'}")
    print(f"来源       : {', '.join(payload['sources'])}")
    print(f"分类       : {', '.join(payload['categories']) or '—'}")
    print(f"链接       : {payload['url'] or payload['oa_url'] or '—'}")
    print(f"首次入库   : {payload['first_seen']}")
    print(f"L1         : {payload['l1']}   L2: {payload['l2']}")
    if detail:
        print(f"  术语     : 标题 {detail.get('title_terms')} / 摘要 {detail.get('abstract_terms')}")
        print("  分项     : " + " ".join(
            f"{k}={detail.get(k)}" for k in ("lex", "cat", "venue", "rec", "seed", "hit")
        ))
    if payload["decision"]:
        print(f"我的判定   : {payload['decision']['decision']} "
              f"({payload['decision']['decided_at'][:10]})")
    print()
    print((payload["abstract"] or "(无摘要)")[:1200])
    return 0


def cmd_eval(args, env) -> int:
    golden = resolve_golden(args.golden)
    if golden is None:
        raise SystemExit(
            "找不到金标集。请把 tests/golden.example.yaml 复制成 tests/golden.yaml 并填好。"
        )
    profile = load_profile(args.profile, ROOT)
    with Store(resolve_db(env)) as store:
        if store.stats()["papers"] == 0:
            raise SystemExit("账本是空的，先跑 `papers.py census` 或 `papers.py import-census`")
        result = evaluate(store, golden, run_id=args.run, profile=profile)

    if args.json:
        emit(result, True)
    else:
        print(format_report(result))
        if golden.endswith("golden.example.yaml"):
            print("⚠️  注意：你正在评测的是**模板**金标集，不是你自己标注的那份。")
            print("    复制为 tests/golden.yaml 并补全 25 正 + 25 负后，指标才有意义。")
    return 0


def cmd_stats(args, env) -> int:
    with Store(resolve_db(env)) as store:
        stats = store.stats()
        runs = [
            {
                "run_id": row["run_id"], "mode": row["mode"], "started": row["started"],
                "fetched": row["n_fetched"], "new": row["n_new"], "note": row["note"],
            }
            for row in store.conn.execute(
                "SELECT * FROM runs ORDER BY run_id DESC LIMIT 10"
            ).fetchall()
        ]
    if args.json:
        emit({"stats": stats, "recent_runs": runs}, True)
        return 0
    print("账本统计")
    print("=" * 46)
    for key in ("papers", "with_abstract", "aliases", "runs", "decisions", "scored", "llm_cached"):
        print(f"  {key:<13} {stats[key]}")
    print(f"  按年份        {stats['by_year']}")
    print(f"  按来源        {stats['by_source']}")
    print()
    print("最近运行")
    for run in runs:
        print(f"  #{run['run_id']:<4} {run['mode']:<8} {run['started'][:19]} "
              f"取回 {run['fetched']} 新增 {run['new']}")
    return 0


# ── 参数解析 ────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="papers.py",
        description="paper-finder：论文分诊工具（粗筛免费，精筛按预算）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"paper-finder {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, with_profile=True):
        if with_profile:
            p.add_argument("--profile",
                           default=os.environ.get("PAPER_FINDER_PROFILE", DEFAULT_PROFILE),
                           help="profile 名字或路径")
        p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
        return p

    def run_opts(p):
        p.add_argument("--limit", type=int, default=100, help="每个查询每个源最多取多少条")
        p.add_argument("--since", default=None, help="只看该日期之后的论文（YYYY 或 YYYY-MM-DD）")
        p.add_argument("--min-year", dest="min_year", type=int, default=None, help="年份下限")
        p.add_argument("--sources", default=None, help="逗号分隔，如 arxiv,crossref")
        p.add_argument("--llm", action="store_true", help="启用 L2 语义精排（需 LLM_API_KEY）")
        p.add_argument("--llm-top", dest="llm_top", type=int, default=40,
                       help="L2 只对 L1 前 N 篇执行（成本闸门，默认 40）")
        p.add_argument("--max-per-bucket", dest="max_per_bucket", type=int, default=10,
                       help="简报每个区块最多渲染多少行，0 表示不限（默认 10）")
        p.add_argument("--stdout", action="store_true", help="把简报打到标准输出而不写文件")
        return p

    p = common(sub.add_parser("doctor", help="自检环境、配置与数据源"))
    p.add_argument("--offline", action="store_true", help="跳过网络连通性检查")
    p.add_argument("--sources", default=None, help="逗号分隔的数据源")
    p.set_defaults(func=cmd_doctor)

    p = common(sub.add_parser("import-census", help="导入冷启动账本 seed-census.json"))
    p.add_argument("--file", default=os.path.join("data", "seed-census.json"))
    p.set_defaults(func=cmd_import)

    p = common(sub.add_parser("dump", help="导出前 N 名候选（含摘要），供 agent 写简介"))
    p.add_argument("--run", type=int, default=None)
    p.add_argument("--top", type=int, default=15, help="导出前多少名，默认 15")
    p.add_argument("--chars", type=int, default=700, help="摘要截断到多少字符")
    p.add_argument("--only-missing", dest="only_missing", action="store_true",
                   help="只导出还没有简介的")
    p.set_defaults(func=cmd_dump)

    p = common(sub.add_parser("annotate", help="把简介写回账本（JSONL 输入）"))
    p.add_argument("file", nargs="?", default="-", help="JSONL 文件路径，- 表示标准输入")
    p.add_argument("--source", default="agent", help="来源标记，默认 agent")
    p.set_defaults(func=cmd_annotate)

    p = common(sub.add_parser("seed-sync", help="按 ID 精确抓取 profile 里的种子论文（补召回漏洞）"))
    p.set_defaults(func=cmd_seed_sync)

    p = common(sub.add_parser("latest", help="打开/打印最近一份日报（每天看这个就行）"), with_profile=False)
    p.add_argument("--profile", default=None, help="只看某个方向；默认两个方向都列")
    p.add_argument("--open", action="store_true", help="用系统默认应用打开日报")
    p.set_defaults(func=cmd_latest)

    p = common(sub.add_parser("census", help="Tier A 全量普查（把领域一次读完）"))
    run_opts(p)
    p.add_argument(
        "--tier", choices=["census", "all", "search"], default="census",
        help="census=Tier A 核心（默认，可穷举、给你的是一份能读完的清单）；"
             "search=Tier B 邻域；all=全部混在一起（会产生大量噪声，慎用）",
    )
    p.add_argument("--no-generation", action="store_true", help="跳过术语漂移世代查询")
    p.add_argument("--no-category", action="store_true", help="跳过分类探针")
    p.set_defaults(func=cmd_census)

    p = common(sub.add_parser("search", help="临时检索一个具体问题"))
    p.add_argument("query")
    run_opts(p)
    p.set_defaults(func=cmd_search)

    p = common(sub.add_parser("watch", help="增量追踪：只看最近新出现的论文"))
    p.add_argument("--days", type=int, default=14, help="回溯天数，默认 14")
    run_opts(p)
    p.set_defaults(func=cmd_watch)

    p = common(sub.add_parser("collect", help="整本会议采集（按 profile.conferences 拉某年年鉴）"))
    run_opts(p)
    p.add_argument("--force", action="store_true", help="已采集过的会议也重采")
    p.add_argument("--out", default=None, help="简报输出路径")
    p.set_defaults(func=cmd_collect)

    p = common(sub.add_parser("daily", help="每日采集：arXiv 增量 + 会议补录，出一份带日期的日报"))
    p.add_argument("--days", type=int, default=3, help="arXiv 回溯天数，默认 3")
    run_opts(p)
    p.add_argument("--force", action="store_true", help="已采集过的会议也重采")
    p.add_argument("--no-conferences", dest="no_conferences", action="store_true", help="跳过会议补录")
    p.add_argument("--show-maybe", dest="hide_maybe", action="store_false",
                   help="也渲染「值得扫一眼」区块（默认只给最相关的 10 篇）")
    p.add_argument("--out", default=None, help="日报输出路径（默认 briefs/daily-<profile>-<date>.md）")
    p.set_defaults(func=cmd_daily, hide_maybe=True)

    p = common(sub.add_parser("rescore", help="重算分数（改完 profile 用，默认覆盖整个账本）"))
    p.add_argument("--run", type=int, default=None, help="只重算该次运行的论文，默认整个账本")
    p.add_argument("--llm", action="store_true")
    p.add_argument("--llm-top", dest="llm_top", type=int, default=40)
    p.set_defaults(func=cmd_rescore)

    p = common(sub.add_parser("brief", help="由某次运行生成 Markdown 简报"))
    p.add_argument("--run", type=int, default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--max-per-bucket", dest="max_per_bucket", type=int, default=40)
    p.add_argument("--archived-preview", dest="archived_preview", type=int, default=10)
    p.set_defaults(func=cmd_brief)

    p = common(sub.add_parser("decide", help="把简报里的判定回写账本"))
    p.add_argument("brief", nargs="?", default=None, help="简报文件路径")
    p.add_argument("--pid", default=None, help="直接指定论文 id（用于救回归档论文）")
    p.add_argument("--decision", default=None, help="k / m / d")
    p.add_argument("--reason", default=None)
    p.set_defaults(func=cmd_decide)

    p = common(sub.add_parser("show", help="查看单篇论文详情"))
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = common(sub.add_parser("eval", help="用金标集评测召回与精确率"))
    p.add_argument("--golden", default=None)
    p.add_argument("--run", type=int, default=None)
    p.set_defaults(func=cmd_eval)

    p = common(sub.add_parser("stats", help="账本统计"))
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env = load_env(ROOT)
    try:
        return args.func(args, env)
    except KeyboardInterrupt:
        print("\n已中断。账本已提交的部分不会丢失。", file=sys.stderr)
        return 130
    except (RateLimited, HttpError) as exc:
        print(f"\n网络错误：{exc}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as exc:
        print(f"\n未预期的错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("如果这是 bug，请把上面的命令和这条信息一起反馈。", file=sys.stderr)
        if os.environ.get("PAPER_FINDER_DEBUG"):
            raise
        return 3


if __name__ == "__main__":
    sys.exit(main())
