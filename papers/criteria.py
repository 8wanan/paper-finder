"""准线（criteria）—— 把"什么该推"从代码里搬到一份可编辑的 Markdown。

为什么要有这层：方向是用户的事，不是代码的事。用户改一句"代码审计优先"，
就该只改一份文档，而不是让我改 profiles、重新跑、再解释。所以：

  · 准线是 `profiles/准线.md`，人可读可改，按「主线 → 必中/加分/扣分/排除」组织；
  · 程序每次运行都读它，算出内容哈希；哈希变了就重新套用（日志里会说）；
  · 套用时**只覆盖打分相关的字段**（标题门、扣分词、排除词、加分词），
    不动查询、会议清单、阈值 —— 那些仍然由 profile 管。

设计取舍：不做通用 Markdown 解析，只认「## 主线」和「### 必中/加分/扣分/排除」
两种标题。因为这是给人写的文件，结构必须一眼看懂，而不是 YAML 那样一改就缩进报错。
"""

from __future__ import annotations

import hashlib
import os
import re

__all__ = ["CRITERIA_FILE", "load_criteria", "apply", "digest", "sections_for"]

CRITERIA_FILE = os.path.join("profiles", "准线.md")

_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_BULLET = re.compile(r"^[-*]\s+(.+)$")


def _clean_item(raw: str) -> str:
    text = raw.strip().strip("`").strip()
    text = text.rstrip(",;。；").strip()
    return text.lower()


def _split_items(block: str) -> list:
    """把一段文本切成词条：支持逗号分隔，也支持一行一个。"""
    items = []
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(">"):
            continue
        if line.startswith(("-", "*")):
            line = _BULLET.sub(r"\1", line)
        for piece in re.split(r"[,，;；]", line):
            item = _clean_item(piece)
            if item and len(item) >= 2:
                items.append(item)
    # 去重但保持顺序
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def load_criteria(path: str) -> dict:
    """解析准线文件，返回 {"vuln": {...}, "methodology": {...}}。"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    result = {}
    current_key, current_bucket = None, None
    buffer = []

    def flush():
        nonlocal buffer
        if current_key and current_bucket and buffer:
            result[current_key].setdefault(current_bucket, [])
            result[current_key][current_bucket].extend(_split_items("\n".join(buffer)))
        buffer = []

    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            level, title = heading.group(1), heading.group(2)
            flush()
            if level == "##":
                if "挖洞" in title:
                    current_key = "vuln"
                elif "方法论" in title:
                    current_key = "methodology"
                else:
                    current_key = None
                if current_key:
                    result.setdefault(current_key, {})
                current_bucket = None
            else:
                bucket = None
                if "必中" in title:
                    bucket = "required"
                elif "加分" in title:
                    bucket = "boost"
                elif "扣分" in title:
                    bucket = "penalty"
                elif "排除" in title:
                    bucket = "exclude"
                current_bucket = bucket
            continue
        if current_bucket:
            buffer.append(line)

    flush()
    # 合并重复桶
    for key, buckets in result.items():
        for name, values in list(buckets.items()):
            buckets[name] = _split_items("\n".join(values))
    return result


def sections_for(profile_name: str) -> str | None:
    """profile 名 → 准线里的主线键。"""
    name = (profile_name or "").lower()
    if "vuln" in name:
        return "vuln"
    if "method" in name:
        return "methodology"
    return None


def digest(path: str) -> str | None:
    """准线内容的短哈希（用于判断"这次和上次是否不同"）。"""
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return hashlib.sha1(handle.read()).hexdigest()[:12]


def apply(profile, criteria: dict | None = None, path: str | None = None) -> bool:
    """把准线套到 profile 上（原地修改）。返回是否发生了变更。"""
    if criteria is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(profile.path or ".")))
        criteria = load_criteria(path or os.path.join(base, CRITERIA_FILE))
    key = sections_for(getattr(profile, "name", ""))
    if not key or key not in criteria:
        return False

    section = criteria[key]
    scoring = profile.data.setdefault("scoring", {})
    changed = False

    required = section.get("required") or []
    if required and scoring.get("title_required_any") != required:
        scoring["title_required_any"] = list(required)
        changed = True

    penalty = section.get("penalty") or []
    if penalty:
        existing = scoring.get("penalty_terms") or {}
        merged = list(dict.fromkeys(list(existing.get("high") or []) + penalty))
        if merged != list(existing.get("high") or []):
            scoring["penalty_terms"] = {**existing, "high": merged}
            changed = True

    exclude = section.get("exclude") or []
    if exclude:
        negative = profile.data.setdefault("negative", {})
        terms = list(dict.fromkeys(list(negative.get("terms") or []) + exclude))
        negative["terms"] = terms
        negative.setdefault("scope", "title")
        changed = True

    boost = section.get("boost") or []
    if boost:
        task = scoring.get("task_terms") or {}
        mid = list(dict.fromkeys(list(task.get("mid") or []) + boost))
        task["mid"] = mid
        scoring["task_terms"] = task
        changed = True

    return changed
