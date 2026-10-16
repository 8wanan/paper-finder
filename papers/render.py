"""Markdown 简报渲染 —— 你实际面对的界面。

设计目标（DESIGN.md §6）：**只读这一张表，不需要点开任何论文**。
所以每一行都必须给出「为什么相关」；在 L1-only 模式下也要如实说明
依据是关键词命中，而不是编造一个看起来像语义判断的理由。

决策列是闭环的入口：你写一个字母，`papers.py decide` 读回去。
短 id 与完整 pid 的映射会写进 sidecar JSON，这样你在简报里
改标题、调行序都不会让回写错位。
"""

from __future__ import annotations

import json
import os

from .normalize import clean_text, short_id as short_id_for
from .store import SUBSTANTIVE_MODES

__all__ = ["render_brief", "short_id_for", "write_sidecar", "SIDECAR_SUFFIX"]

SIDECAR_SUFFIX = ".pids.json"

_BUCKET_TITLES = {
    "strong": "🔥 强烈建议读",
    "maybe": "📌 值得扫一眼",
    "review": "🆕 需人工确认（无摘要）",
    "archived": "🗄 已归档",
}


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


def _row_markdown(row, short_id: str, has_llm: bool, is_new: bool) -> str:
    title = _escape_cell(row["title"]) or "(无标题)"
    url = row["url"] or row["oa_url"] or ""
    title_cell = f"[{title}]({url})" if url else title

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
    return f"| `{short_id}` | {new_cell} | {title_cell} | {year} | {l1_cell} | {basis} | [ ] |"


_HEADER = (
    "| id | 新 | 标题 | 年 | 分数 | 为什么相关 | 决策 |\n"
    "|----|----|------|----|------|-----------|------|"
)


def render_brief(store, profile, run_id=None, has_llm=False, archived_preview=10,
                 max_per_bucket=None, reviewer=None):
    """生成 Markdown 简报，返回 (markdown, id_map, stats)。

    max_per_bucket：每个展开区块最多渲染多少行。search 模式一次可能带回上百篇
    候选，简报必须保持"5 分钟能读完"的长度，所以按分数截断并如实报告被截断的数量。

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

    rows = store.run_paper_rows(run["run_id"])
    if reviewer is None:
        from .screen import bucket_of

        reviewer = lambda row: bucket_of(row, profile, has_llm)  # noqa: E731

    buckets = {"strong": [], "maybe": [], "review": [], "archived": []}
    for row in rows:
        buckets[reviewer(row)].append(row)

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
    lines.append(f"# 语音安全论文简报 · {run['started'][:10]}")
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
    else:
        lines.append(
            "筛选模式：**L1-only 粗筛**（未配置 LLM key，未做语义精排）。"
            "「为什么相关」一栏显示的是关键词命中事实，不是语义判断。"
        )
    lines.append("")
    lines.append(
        "> 在最后一列把 `[ ]` 改成 `[k]`(读) / `[m]`(稍后) / `[d]`(丢弃)，"
        "然后执行 `papers.py decide <本文件>` 回写判定。"
        "不执行这一步，工具就无法从你的判断里学习。"
    )
    lines.append("")

    for key in ("strong", "maybe", "review"):
        items = buckets[key]
        lines.append(f"## {_BUCKET_TITLES[key]}（{len(items)} 篇）")
        lines.append("")
        if not items:
            lines.append("_本轮无。_")
            lines.append("")
            continue
        shown = items if not max_per_bucket else items[:max_per_bucket]
        lines.append(_HEADER)
        for row in shown:
            lines.append(
                _row_markdown(row, reverse[row["pid"]], has_llm, bool(row["is_new"]))
            )
        if len(items) > len(shown):
            lines.append("")
            lines.append(
                f"_另有 {len(items) - len(shown)} 篇同档论文未展开"
                f"（用 `--max-per-bucket 0` 全量输出）。_"
            )
        lines.append("")

    archived = buckets["archived"]
    lines.append(f"## {_BUCKET_TITLES['archived']}（{len(archived)} 篇，默认折叠）")
    lines.append("")
    if archived:
        lines.append(
            f"这些论文分数低于阈值，被自动下沉。若其中有你想要的，"
            f"用 `papers.py decide --pid <id> k` 手动救回。"
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
        "mode": "l1+l2" if has_llm else "l1-only",
    }
    return "\n".join(lines), id_map, stats


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
