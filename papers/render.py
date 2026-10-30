"""Markdown 简报渲染 —— 你实际面对的界面。

设计目标（DESIGN.md §6）：**只读这一张表，不需要点开任何论文**。
所以每一行都必须给出「为什么相关」；在 L1-only 模式下也要如实说明
依据是关键词命中，而不是编造一个看起来像语义判断的理由。

决策列是闭环的入口：你写一个字母，`papers.py decide` 读回去。
短 id 与完整 pid 的映射会写进 sidecar JSON，这样你在简报里
改标题、调行序都不会让回写错位。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re

from .normalize import clean_text, short_id as short_id_for
from .store import SUBSTANTIVE_MODES

__all__ = ["render_brief", "short_id_for", "write_sidecar", "SIDECAR_SUFFIX", "row_date"]

SIDECAR_SUFFIX = ".pids.json"

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

_BUCKET_TITLES = {
    "strong": "🔥 强烈建议读",
    "maybe": "📌 值得扫一眼",
    "review": "🆕 需人工确认（无摘要或疑似注入）",
    "archived": "📦 其余候选（未展开）",
}


def row_date(row):
    """取一篇论文的"最新可能日期"，用于判断它够不够新。

    有精确发表日期就用它；只有年份时取**该年 12 月 31 日**（宽松口径）：
    宁可把年末的论文算新，也不要把一篇 12 月发表的顶会论文误判成旧的。
    """
    published = row["published"] if not isinstance(row, dict) else row.get("published")
    match = _DATE_RE.match(clean_text(published))
    if match:
        try:
            return _dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            pass
    year = row["year"] if not isinstance(row, dict) else row.get("year")
    if year:
        try:
            return _dt.date(int(year), 12, 31)
        except (TypeError, ValueError):
            return None
    return None


def drop_stale(buckets, max_age_days, conference_max_age_days=None, today=None):
    """「只要新的」：过期论文从展示桶挪进归档桶。

    两个窗口（实测教训）：预印本的"新"是**天**级（arXiv 每天都有新货），
    而会议论文的"新"是**会议周期**级 —— 一篇 4 个月前在 S&P 上发表的论文，
    对读者仍是新的；用 30 天窗口一刀切会把整本会议都判成旧的。
    所以默认给会议论文一个更宽的窗口。

    为什么放在渲染层而不是打分层：新鲜度是**展示口径**，不是相关性判断。
    打分层保留全量排序（历史论文仍可查、可被 `decide --pid` 救回），
    只是每天的日报不把它们排进来。
    """
    if not max_age_days and not conference_max_age_days:
        return 0
    from . import venues

    today = today or _dt.date.today()
    narrow = today - _dt.timedelta(days=int(max_age_days or 0))
    wide = today - _dt.timedelta(days=int(conference_max_age_days or max_age_days or 0))
    moved = 0
    for key in ("strong", "maybe", "review"):
        keep = []
        for row in buckets[key]:
            when = row_date(row)
            venue = row["venue"] if not isinstance(row, dict) else row.get("venue")
            cutoff = wide if venues.status(venue) == "conference" else narrow
            if when is not None and when < cutoff:
                buckets["archived"].append(row)
                moved += 1
            else:
                keep.append(row)
        buckets[key] = keep
    return moved


def _in_scope(row, profile) -> bool:
    """展示前再判一次 L0（venue 口径 + 标题级负向词）。

    为什么要重复判：L0 只在**入库那一刻**生效，而口径是会改的（今天就把
    "医用影像代码生成""晶圆机器人 fuzzing"这类跑题论文加进了负向词）。
    如果不在展示层再判一次，改完配置后旧数据仍然会出现在日报里，
    用户会以为配置根本没生效（实测踩到）。
    """
    from .screen import passes_l0

    paper = {
        "title": row["title"],
        "abstract": row["abstract"],
        "venue": row["venue"],
        "year": row["year"],
        "published": row["published"],
        "categories": json.loads(row["categories"]) if row["categories"] else [],
    }
    ok, _reason = passes_l0(paper, profile)
    return ok


def build_id_map(rows) -> dict:
    """构造 短id -> 完整pid 的映射，并保证短 id 唯一。"""
    mapping = {}
    used = {}
    for row in rows:
        pid = row["pid"]
        base = short_id_for(pid)
        candidate = base
        if candidate in mapping and mapping[candidate] != pid:
            # 短 id 冲突：加一个稳定后缀
            suffix = 2
            while f"{base}~{suffix}" in mapping:
                suffix += 1
            candidate = f"{base}~{suffix}"
        mapping[candidate] = pid
        used[candidate] = pid
    return mapping


def ranked_rows_for_run(store, run_id, profile):
    """返回 (rows, rank_of)。

    排序与编号规则集中在这里，供 render_brief 与 `papers.py dump` 共用 ——
    两处若各写一份，"导出的顺序"和"简报的顺序"迟早会不一致。

    排序键：**有核心信号**的排前面，然后按分数。

    重要：这里的 L1 **按传入的 profile 现算**，不读库里的 papers.l1。
    原因（实测踩到）：一个账本服务多个方向，而 papers.l1 只有一列 ——
    跑过一次方法论的 rescore，就会把挖洞方向的分数覆盖成方法论的尺度，
    于是同一份日报的两个方向互相污染。现算 4500 篇约 0.2 秒，换来正确的隔离。
    """
    from .screen import is_review_row, score_l1

    counts = {
        row["pid"]: row["n"]
        for row in store.conn.execute(
            "SELECT pid, COUNT(*) AS n FROM paper_queries GROUP BY pid"
        ).fetchall()
    }

    rows = []
    for raw in store.run_paper_rows(run_id):
        row = dict(raw)
        paper = {
            "title": row.get("title"),
            "abstract": row.get("abstract"),
            "categories": json.loads(row["categories"]) if row.get("categories") else [],
            "authors": json.loads(row["authors"]) if row.get("authors") else [],
            "sources": json.loads(row["sources"]) if row.get("sources") else [],
            "year": row.get("year"),
            "published": row.get("published"),
            "venue": row.get("venue"),
            "arxiv_id": row.get("arxiv_id"),
        }
        score, detail = score_l1(paper, profile, query_hits=counts.get(row["pid"], 0))
        row["l1"] = score
        row["l1_detail"] = json.dumps(detail, ensure_ascii=False)
        rows.append(row)

    def sort_key(row):
        detail = row["l1_detail"]
        core = False
        if detail:
            try:
                core = bool((json.loads(detail) or {}).get("has_core_signal"))
            except (json.JSONDecodeError, TypeError):
                core = False
        return (
            -int(core),                                                   # 核心信号优先
            -(row["l2"] if row["l2"] is not None else -1),                # 再按 L2
            -(row["l1"] if row["l1"] is not None else -1),                # 再按 L1
            -(row["year"] or 0),
        )

    rows.sort(key=sort_key)
    rankable = [r for r in rows if not is_review_row(r, profile)]
    rank_of = {row["pid"]: index + 1 for index, row in enumerate(rankable)}
    return rows, rank_of


def _dedupe(values):
    """按出现顺序去重，避免简报里出现 "adversarial、adversarial、jailbreak"。"""
    seen, out = set(), []
    for value in values or []:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def explain_l1(detail: dict | None) -> str:
    """把 L1 明细翻译成一句人话。绝不夸大：这里只说关键词命中事实。"""
    if not detail:
        return ""
    parts = []
    title_terms = _dedupe(detail.get("title_terms"))
    abstract_terms = _dedupe(detail.get("abstract_terms"))
    if title_terms:
        parts.append("标题命中 " + "、".join(f"`{t}`" for t in title_terms[:4]))
    if abstract_terms:
        parts.append("摘要命中 " + "、".join(f"`{t}`" for t in abstract_terms[:4]))
    if detail.get("is_seed"):
        parts.append("种子论文")
    if not parts:
        return "仅分类/时间加权，无关键词命中"
    return "；".join(parts)


def _escape_cell(text: str) -> str:
    return clean_text(text).replace("|", "\\|")


def _venue_cell(row) -> str:
    """简报的「会议」列：已中会显示归一化后的会议名，未发表显示「预印本」。"""
    from . import venues as venues_mod

    venue = row["venue"] if not isinstance(row, dict) else row.get("venue")
    key = venues_mod.canonical(venue)
    if key:
        return _escape_cell(venues_mod.display(key))
    if venues_mod.is_preprint(venue):
        return "预印本"
    return _escape_cell(str(venue)[:40])


def _row_markdown(row, short_id: str, has_llm: bool, is_new: bool, annotation=None,
                  show_venue: bool = False, summary_layout: bool = False) -> str:
    title = _escape_cell(row["title"]) or "(无标题)"
    url = row["url"] or row["oa_url"] or ""
    title_cell = f"[{title}]({url})" if url else title
    venue_cell = f" {_venue_cell(row)} |" if show_venue else ""

    year = row["year"] if row["year"] is not None else "—"
    l1 = row["l1"]
    l1_cell = f"{l1:.2f}" if l1 is not None else "—"

    detail = None
    if row["l1_detail"]:
        try:
            detail = json.loads(row["l1_detail"])
        except (json.JSONDecodeError, TypeError):
            detail = None

    # 被主动跳过 L2 的论文（无摘要 / 疑似 prompt injection）要如实说明原因，
    # 否则用户会以为"没有语义判断"是系统故障而不是刻意的安全选择。
    llm_payload = {}
    if row["llm_json"]:
        try:
            llm_payload = json.loads(row["llm_json"]) or {}
        except (json.JSONDecodeError, TypeError):
            llm_payload = {}
    skipped = llm_payload.get("skipped") if isinstance(llm_payload, dict) else None

    if has_llm and row["l2"] is not None:
        reason = _escape_cell(llm_payload.get("reason") or "")
        quote = _escape_cell(llm_payload.get("evidence_quote") or "")
        basis = reason
        if quote:
            basis += f"　<sub>原文：{quote}</sub>"
        l1_cell = f"{l1_cell} / L2 {int(row['l2'])}"
    else:
        basis = _escape_cell(explain_l1(detail))

    if skipped:
        flag = "⚠️ 摘要含疑似指令，已跳过语义精排" if "injection" in str(skipped) else f"⚠️ {skipped}"
        basis = _escape_cell(flag) + (f"；{basis}" if basis else "")

    new_cell = "✦" if is_new else ""

    # 有简介时用它替换掉"关键词命中"那一栏 —— 后者只说明"为什么匹配上"，
    # 不说明"这篇在讲什么"，而后者才是用户决定读不读的依据。
    #
    # 表格是「简介版」但这篇没有简介时，必须补一句占位而不是退回旧列数 ——
    # 否则同一张表里出现 7 列和 8 列两种行，Markdown 会直接错位（实测踩到）。
    if annotation is not None:
        summary = _escape_cell(annotation["summary"])
        why = _escape_cell(annotation["why_relevant"] or "")
        cell = f"{summary}　→　{why}" if why else summary
        return f"| `{short_id}` | {new_cell} | {title_cell} |{venue_cell} {cell} | {year} | [ ] |"

    if summary_layout:
        cell = "（今日未写简介）" + (f"　{basis}" if basis else "")
        return f"| `{short_id}` | {new_cell} | {title_cell} |{venue_cell} {cell} | {year} | [ ] |"

    return f"| `{short_id}` | {new_cell} | {title_cell} |{venue_cell} {year} | {l1_cell} | {basis} | [ ] |"


# 两种表头：有简介时用窄表（简介取代"分数+依据"，那两栏在有简介后是噪声）
_HEADER_PLAIN = (
    "| id | 新 | 标题 | 年 | 分数 | 依据 | 决策 |\n"
    "|----|----|------|----|------|------|------|"
)
_HEADER_SUMMARY = (
    "| id | 新 | 标题 | 简介 | 年 | 决策 |\n"
    "|----|----|------|------|----|------|"
)
# 配了顶会白名单的 profile 多一栏「会议」—— 否则用户看不出这篇是不是顶会，
# 而这正是这套配置存在的理由。
_HEADER_PLAIN_VENUE = (
    "| id | 新 | 标题 | 会议 | 年 | 分数 | 依据 | 决策 |\n"
    "|----|----|------|------|----|------|------|------|"
)
_HEADER_SUMMARY_VENUE = (
    "| id | 新 | 标题 | 会议 | 简介 | 年 | 决策 |\n"
    "|----|----|------|------|------|----|------|"
)


def _headers(show_venue: bool):
    if show_venue:
        return _HEADER_PLAIN_VENUE, _HEADER_SUMMARY_VENUE
    return _HEADER_PLAIN, _HEADER_SUMMARY


def render_brief(store, profile, run_id=None, has_llm=False, archived_preview=10,
                 max_per_bucket=None, reviewer=None, include_maybe=True):
    """生成 Markdown 简报，返回 (markdown, id_map, stats)。

    max_per_bucket：每个展开区块最多渲染多少行。search 模式一次可能带回上百篇
    候选，简报必须保持"5 分钟能读完"的长度，所以按分数截断并如实报告被截断的数量。

    include_maybe：是否渲染「值得扫一眼」区块。日报把它关掉 —— 用户只想要
    最相关的几篇，多出来的"This 也相关"只会稀释注意力。

    run_id 为空时取最近一次**有实质内容的**运行（跳过 import / seed-sync 记账）。
    """
    if run_id is not None:
        run = store.get_run(run_id)
    else:
        run = store.latest_run(modes=SUBSTANTIVE_MODES)
    if run is None:
        existing = store.stats()["papers"]
        lead = (
            f"账本里已有 {existing} 篇论文，但还没跑过检索类运行。"
            if existing
            else "账本是空的。"
        )
        return (
            "# 简报\n\n"
            f"{lead}\n\n"
            "跑其中一条即可生成简报：\n"
            "- `papers.py census` 全量普查你的领域（推荐第一次用这个）\n"
            "- `papers.py watch --days 7` 只看最近新增\n"
            "- `papers.py search \"<关键词>\"` 临时查一个具体问题\n",
            {},
            {},
        )

    rows, rank_of = ranked_rows_for_run(store, run["run_id"], profile)
    from .screen import bucket_of

    if reviewer is None:
        reviewer = lambda row, rank: bucket_of(row, profile, has_llm, rank=rank)  # noqa: E731

    buckets = {"strong": [], "maybe": [], "review": [], "archived": []}
    for row in rows:
        buckets[reviewer(row, rank_of.get(row["pid"]))].append(row)

    # 口径可能改过（负向词、venue 白名单）：展示前按当前配置再判一次，
    # 不合规的直接挪进归档，而不是让旧数据继续占位置。
    for key in ("strong", "maybe", "review"):
        buckets[key] = [row for row in buckets[key] if _in_scope(row, profile)]

    # 「只要新的」：把超过 profile.max_age_days 的论文挪进归档，
    # 不删除、不降分，只是不出现在今天的日报里。
    stale = drop_stale(
        buckets,
        getattr(profile, "max_age_days", None),
        getattr(profile, "conference_max_age_days", None),
    )

    # 已经判定为「丢弃」的论文不再出现 —— 否则每天都要重新划掉同一批人。
    # `m`（稍后）与 `k`（读）仍然保留：它们是"我想读"，不是"别再给我看"。
    dropped = {
        pid for pid, row in store.decisions().items() if row["decision"] == "d"
    }
    hidden_decided = 0
    if dropped:
        for key in ("strong", "maybe", "review"):
            before = len(buckets[key])
            buckets[key] = [row for row in buckets[key] if row["pid"] not in dropped]
            hidden_decided += before - len(buckets[key])

    # 简介：有则用它替代"关键词命中"那一栏
    annotations = store.annotations()
    show_summary = any(row["pid"] in annotations for row in rows)

    for key in buckets:
        buckets[key].sort(
            key=lambda r: (
                -(r["l2"] if r["l2"] is not None else -1),
                -(r["l1"] if r["l1"] is not None else -1),
                -(r["year"] or 0),
            )
        )

    id_map = build_id_map(rows)
    reverse = {v: k for k, v in id_map.items()}

    n_new = sum(1 for row in rows if row["is_new"])
    n_total = store.conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]

    lines = []
    # 标题跟 profile 走：硬编码"语音安全"的版本在换方向后会误导用户
    # （他打开的是 AI 挖洞日报，标题却写着语音安全）。
    lines.append(f"# {profile.display_name} · 论文简报 · {run['started'][:10]}")
    lines.append("")
    lines.append(
        f"运行 #{run['run_id']} · 模式 `{run['mode']}` · profile `{run['profile'] or '—'}`"
    )
    lines.append("")
    lines.append(
        f"本轮候选 **{len(rows)}** 篇 · 其中新增 **{n_new}** 篇 · 账本累计 **{n_total}** 篇"
    )
    lines.append("")
    if has_llm:
        lines.append("筛选模式：**L1 粗筛 + L2 语义精排**")
    elif show_summary:
        lines.append(
            "筛选模式：**L1 关键词粗筛**（未配置 LLM）。"
            "「简介」一栏由 agent 阅读摘要后写出，不是关键词匹配结果。"
        )
    else:
        lines.append(
            "筛选模式：**L1-only 粗筛**（未配置 LLM key，未做语义精排）。"
            "「依据」一栏显示的是关键词命中事实，不是语义判断 —— "
            "跑 `papers.py dump` 后由 agent 写简介，简报会多出「简介」一栏。"
        )
    lines.append("")
    max_age = getattr(profile, "max_age_days", None)
    max_age_conf = getattr(profile, "conference_max_age_days", None)
    if max_age or max_age_conf:
        parts = []
        if max_age_conf:
            parts.append(f"顶会论文近 {int(max_age_conf // 365)} 年")
        if max_age:
            parts.append(f"预印本近 {int(max_age)} 天")
        lines.append(
            "> 展示口径：" + "、".join(parts)
            + "；更早的被移入「其余候选」，仍可 `decide --pid` 找回。"
        )
        lines.append("")
    lines.append(
        "> 在最后一列把 `[ ]` 改成 `[k]`(读) / `[m]`(稍后) / `[d]`(丢弃)，"
        "然后执行 `papers.py decide <本文件>` 回写判定。"
        "不执行这一步，工具就无法从你的判断里学习。"
    )
    lines.append("")

    show_venue = bool(getattr(profile, "venues", None))
    header_plain, header_summary = _headers(show_venue)
    sections = ("strong", "maybe", "review") if include_maybe else ("strong", "review")
    for key in sections:
        items = buckets[key]
        lines.append(f"## {_BUCKET_TITLES[key]}（{len(items)} 篇）")
        lines.append("")
        if not items:
            lines.append("_本轮无。_")
            lines.append("")
            continue
        shown = items if not max_per_bucket else items[:max_per_bucket]
        lines.append(header_summary if show_summary else header_plain)
        for row in shown:
            lines.append(
                _row_markdown(
                    row, reverse[row["pid"]], has_llm, bool(row["is_new"]),
                    annotation=(annotations.get(row["pid"]) if show_summary else None),
                    show_venue=show_venue,
                    summary_layout=show_summary,
                )
            )
        if len(items) > len(shown):
            lines.append("")
            lines.append(
                f"_另有 {len(items) - len(shown)} 篇同档论文未展开"
                f"（用 `--max-per-bucket 0` 全量输出）。_"
            )
        lines.append("")

    archived = buckets["archived"]
    lines.append(f"## {_BUCKET_TITLES['archived']}（{len(archived)} 篇）")
    lines.append("")
    if archived:
        lines.append(
            "这些是名次靠后的候选。**它们是后备队列，不是「已丢弃」** —— "
            "分数在 0.45 以上的仍然值得一查。用 "
            "`papers.py decide --pid <id> k` 可以把任意一篇提到你的队列里。"
        )
        lines.append("")
        for row in archived[:archived_preview]:
            short = reverse[row["pid"]]
            lines.append(f"- `{short}` {clean_text(row['title'])[:110]}")
        if len(archived) > archived_preview:
            lines.append(f"- …另有 {len(archived) - archived_preview} 篇")
    else:
        lines.append("_本轮无。_")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"决策列填写字母：`k` 读 / `m` 稍后 / `d` 丢弃。"
        f"带 ✦ 的是**本轮新增**论文 —— 这是增量追踪的核心产出。"
    )
    lines.append("")

    stats = {
        "run_id": run["run_id"],
        "candidates": len(rows),
        "new": n_new,
        "ledger_total": n_total,
        "buckets": {k: len(v) for k, v in buckets.items()},
        "stale_dropped": stale,
        "decided_hidden": hidden_decided,
        "mode": "l1+l2" if has_llm else "l1-only",
        "summaries": sum(1 for row in rows if row["pid"] in annotations),
    }
    return "\n".join(lines), id_map, stats


def _digest_item(index: int, row, annotation) -> list:
    """合并日报里的一篇：标题（带链接）一行，会议标签 + 一句话简介一行。"""
    title = _escape_cell(row["title"]) or "(无标题)"
    url = row["url"] or row["oa_url"] or ""
    title_cell = f"[{title}]({url})" if url else title

    from . import venues

    venue = row["venue"]
    key = venues.canonical(venue)
    if key:
        tag = f"`{venues.display(key)}`"
    elif venues.is_preprint(venue):
        tag = "预印本"
    else:
        tag = ""

    lines = [f"{index}. **{title_cell}**"]
    if annotation:
        summary = _escape_cell(annotation["summary"])
        body = f"{tag}｜{summary}" if tag else summary
    else:
        detail = None
        if row["l1_detail"]:
            try:
                detail = json.loads(row["l1_detail"])
            except (json.JSONDecodeError, TypeError):
                detail = None
        basis = _escape_cell(explain_l1(detail))
        body = f"{tag}｜（待写简介）{basis}" if tag else f"（待写简介）{basis}"
    lines.append(f"   {body}")
    return lines


def render_digest(store, items, today=None):
    """把多个方向的强推合并成一份「只看标题 + 链接 + 简介」的日报。

    items: [(profile, run_id)]。每个方向取强推桶、按 profile 的窗口过滤新鲜度、
    跳过已判 `d` 的、截断到 strong_top，然后拼成一份文件。

    为什么单独做一份：`brief-*.md` 是**工作台**（带分数、依据、决策列，用于回写判定），
    而用户每天真正要看的是**清单**。两者混在一起，读者会被分数和决策列干扰。
    """
    today = today or _dt.date.today()
    sections, total = [], 0
    seen = set()
    for profile, run_id in items:
        if run_id is None:
            continue
        rows, rank_of = ranked_rows_for_run(store, run_id, profile)
        from .screen import bucket_of, is_review_row

        annotations = store.annotations()
        has_llm = bool(
            store.conn.execute(
                "SELECT 1 FROM run_papers WHERE run_id=? AND l2 IS NOT NULL LIMIT 1",
                (run_id,),
            ).fetchone()
        )
        buckets = {"strong": [], "maybe": [], "review": [], "archived": []}
        for row in rows:
            buckets[bucket_of(row, profile, has_llm, rank=rank_of.get(row["pid"]))].append(row)
        for key in ("strong", "maybe", "review"):
            buckets[key] = [row for row in buckets[key] if _in_scope(row, profile)]

        drop_stale(
            buckets,
            getattr(profile, "max_age_days", None),
            getattr(profile, "conference_max_age_days", None),
            today=today,
        )
        dropped = {pid for pid, row in store.decisions().items() if row["decision"] == "d"}
        if dropped:
            for key in ("strong", "maybe", "review"):
                buckets[key] = [row for row in buckets[key] if row["pid"] not in dropped]

        cap = int(profile.thresholds.get("strong_top") or 10)
        # 两个方向的查询有重叠（同一篇"agent 做漏洞修复"会同时命中两条线），
        # 合并文件里必须去重 —— 前面已经列过的论文不再重复出现，并向后补位。
        #
        # 只用强推桶（+ 需人工确认）：不做"从 maybe 里补位"。补位会把没过
        # 标题门/被扣分的论文重新拉回必读区 —— 那正是今天这次返工要消灭的现象。
        candidates = buckets["strong"] + buckets["review"]
        picked = []
        for row in candidates:
            if row["pid"] in seen:
                continue
            picked.append(row)
            if len(picked) >= cap:
                break
        if not picked:
            continue
        for row in picked:
            seen.add(row["pid"])

        # 顶会优先：用户的口径是"只要是顶会，近期的都可以"，所以同类里先列已中会的。
        from . import venues as venues_mod

        picked.sort(key=lambda r: 0 if venues_mod.status(r["venue"]) == "conference" else 1)

        title = profile.display_name.split("·")[0].strip()
        icon = "🛡️" if "挖洞" in title else ("🧪" if "方法论" in title else "📄")
        lines = [f"## {icon} {title} · {len(picked)} 篇", ""]
        for index, row in enumerate(picked, 1):
            lines.extend(_digest_item(index, row, annotations.get(row["pid"])))
            lines.append("")
        sections.append("\n".join(lines))
        total += len(picked)

    header = [
        "# 每日论文速览",
        "",
        f"**{today.isoformat()}** · 共 **{total}** 篇 · "
        "带 `会议名` 的是已中顶会，写「预印本」的是还没中会的新工作",
        "",
        "> 想标记已读/丢弃：在 `briefs/latest-<方向>.md` 的最后一列填 `k/m/d`，"
        "再跑 `papers.py decide <该文件>`。",
        "",
        "---",
        "",
    ]
    return "\n".join(header + sections)


def write_sidecar(brief_path: str, id_map: dict) -> str:
    """把 短id -> pid 的映射写进 sidecar，供 decide 精确回写。"""
    path = brief_path + SIDECAR_SUFFIX
    payload = {
        "brief": os.path.basename(brief_path),
        "ids": id_map,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    return path
