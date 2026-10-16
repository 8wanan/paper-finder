"""决策回写 —— 反馈闭环的入口。

为什么这一步不能省（DESIGN.md §0.3 判断三）：`decisions` 表是唯一真值来源。
没有它，"评分优化"就没有基准；有了它，才能反推"哪条查询在制造噪声、
哪个 venue 白名单设置错了"。

支持两种输入：
    1. 简报文件：解析表格最后一列的 [k]/[m]/[d]
    2. 显式指定：--pid <短id或完整pid> --decision k
"""

from __future__ import annotations

import json
import os
import re

from .render import SIDECAR_SUFFIX

__all__ = ["parse_brief", "apply_decisions", "load_sidecar"]

_DECISION_RE = re.compile(r"^\[?\s*([kmdKMD])\s*\]?(?:\s+(.*\S))?\s*$")
_ID_CELL_RE = re.compile(r"`([^`]+)`")
_VALID = {"k": "keep", "m": "maybe", "d": "drop"}


def load_sidecar(brief_path: str) -> dict:
    """读取简报旁边的 id 映射；不存在则返回空字典。"""
    path = brief_path + SIDECAR_SUFFIX
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        ids = payload.get("ids") or {}
        return ids if isinstance(ids, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def parse_brief(text: str):
    """从简报 Markdown 里抓出 (短id, 决策字母, 备注)。

    只认表格行，且必须能在第一列找到反引号包裹的 id、在最后一列看到
    [k]/[m]/[d]。这样即使你手改标题、调行序、加备注也不会错位。

    决策列允许带备注，例如 `[k] 综述必引` —— 备注会写进 decisions.reason。
    这是刻意支持的：写备注比只写一个字母有用得多，而"必须严格只写一个字母"
    的规则没人会遵守。
    """
    results = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 3:
            continue
        id_match = _ID_CELL_RE.search(cells[0])
        if not id_match:
            continue
        decision_match = _DECISION_RE.match(cells[-1])
        if not decision_match:
            continue
        note = (decision_match.group(2) or "").strip() or None
        results.append((id_match.group(1), decision_match.group(1).lower(), note))
    return results


def apply_decisions(store, entries, id_map=None, log=None):
    """把 (短id, 字母, 备注) 写进 decisions 表。

    返回 (已应用列表, 未解析列表)。未解析不是错误 —— 你很可能只标了其中几行。
    """
    log = log or (lambda msg: None)
    id_map = id_map or {}
    applied, unresolved = [], []

    for entry in entries:
        short, letter = entry[0], entry[1]
        note = entry[2] if len(entry) > 2 else None
        pid = id_map.get(short)
        if pid is None:
            pid = store.resolve_pid(short)
        if pid is None:
            unresolved.append(short)
            continue
        store.set_decision(pid, _VALID[letter], reason=note)
        applied.append((short, pid, _VALID[letter]))
        log(f"  · {short} -> {_VALID[letter]}" + (f"（{note}）" if note else ""))
    return applied, unresolved


def apply_explicit(store, pid_or_short, letter, reason=None, tags=None):
    """显式指定一篇论文的判定，并支持救回被归档的论文。"""
    letter = letter.strip().lower()
    if letter not in _VALID:
        raise ValueError(f"决策只能是 k / m / d，收到 {letter!r}")
    pid = store.resolve_pid(pid_or_short)
    if pid is None:
        raise LookupError(f"账本里找不到论文：{pid_or_short}")
    store.set_decision(pid, _VALID[letter], reason=reason, tags=tags)
    return pid
