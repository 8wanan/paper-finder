"""会议名归一化 —— 把「顶会」从一个字符串变成可判定的集合。

为什么需要它（实测踩坑）：

1. **同名会议在不同数据源里的写法完全不同**。同一个 ICSE 2025，OpenAlex 给的
   `venue` 是 "ACM/IEEE International Conference on Software Engineering:
   Companion Proceedings (ICSE-Companion)"，USENIX 官网给的是
   "USENIX Security Symposium"，arXiv 的 journal_ref 又可能是任意作者手写格式。
   直接做字符串包含匹配，要么漏（"ICSE-Companion" 匹配不到 "ICSE" 白名单），
   要么误中（"ASE" 会命中 "database" 里的 ase）。

2. **venue 在旧方案里只是 0.10 权重的打分特征**，不是过滤条件。要做"只收顶会"
   就必须有一个能回答"这篇是不是顶会"的函数，且答案要可解释（给出归一化后的
   会议 key），这样过滤和简报显示才能共用同一套判断。

设计取舍：
- 每个会议给一组**正则**（带词边界），而不是一行别名表。因为别名表要靠子串
  匹配，短缩写必然误命中。
- 归一化只认白名单里的会议；不认识的 venue 返回 None，由调用方决定怎么处理
  （顶会模式下拒掉，普通模式下按"其他"档次打分）。
- 顺序敏感：先匹配更具体的（EuroS&P 先于 S&P，ASE 先于「任何含 ase 的词」）。
"""

from __future__ import annotations

import re

from .normalize import clean_text

__all__ = [
    "canonical",
    "display",
    "status",
    "is_preprint",
    "known_keys",
    "check_venue_policy",
    "VENUES",
]

# (key, 显示名, 正则列表)
# 顺序即优先级：越具体的越靠前。
VENUES = [
    ("euros&p", "EuroS&P", [r"euros\s*&\s*p", r"european symposium on security and privacy"]),
    ("usenix", "USENIX Security", [r"usenix security", r"usenix security symposium"]),
    ("ndss", "NDSS", [r"\bndss\b", r"network and distributed system security"]),
    ("sp", "IEEE S&P", [r"\bs\s*&\s*p\b", r"security and privacy"]),
    ("ccs", "ACM CCS", [r"\bccs\b", r"computer and communications security"]),
    ("icse", "ICSE", [r"\bicse\b", r"international conference on software engineering"]),
    ("fse", "FSE", [r"\bfse\b", r"esec/fse", r"foundations of software engineering"]),
    ("issta", "ISSTA", [r"\bissta\b", r"software testing and analysis"]),
    ("ase", "ASE", [r"\base\b", r"automated software engineering"]),
    ("neurips", "NeurIPS", [r"\bneurips\b", r"neural information processing systems"]),
    ("icml", "ICML", [r"\bicml\b", r"international conference on machine learning"]),
    ("iclr", "ICLR", [r"\biclr\b", r"learning representations"]),
    ("acl", "ACL", [r"\bacl\b", r"association for computational linguistics"]),
    ("emnlp", "EMNLP", [r"\bemnlp\b", r"empirical methods in natural language processing"]),
    ("naacl", "NAACL", [r"\bnaacl\b", r"north american chapter of the association"]),
    ("coling", "COLING", [r"\bcoling\b", r"international conference on computational linguistics"]),
    ("aaai", "AAAI", [r"\baaai\b"]),
    ("ijcai", "IJCAI", [r"\bijcai\b"]),
    ("tdsc", "IEEE TDSC", [r"\btdsc\b", r"dependable and secure computing"]),
    ("tse", "IEEE TSE", [r"\btse\b", r"transactions on software engineering"]),
    ("tosem", "ACM TOSEM", [r"\btosem\b", r"transactions on software engineering and methodology"]),
]

DISPLAY = {key: name for key, name, _patterns in VENUES}
_COMPILED = [(key, name, [re.compile(p, re.I) for p in patterns]) for key, name, patterns in VENUES]

_PREPRINT_RE = re.compile(r"arxiv|cornell university|\bcorr\b|preprint|techrxiv|ssrn", re.I)


def canonical(venue) -> str | None:
    """把任意写法的会议名归一化成 key；不认识返回 None。

    >>> canonical("ACM/IEEE International Conference on Software Engineering: Companion Proceedings (ICSE-Companion)")
    'icse'
    >>> canonical("USENIX Security Symposium 2025")
    'usenix'
    >>> canonical("usenix")          # profile 里直接写 key 也得认
    'usenix'
    >>> canonical("IEEE Access") is None
    True
    """
    text = clean_text(venue)
    if not text:
        return None
    lowered = text.strip().lower()
    # 先认 key 本身：profile 的 venues / conferences 里写的是 ccs、sp、usenix
    # 这类短 key，而不是会议全称。少这一步，canonical("usenix") 会是 None，
    # 于是 profile 里的 4 个会议条目会被静默丢掉（实测踩到）。
    if lowered in DISPLAY:
        return lowered
    for key, _name, patterns in _COMPILED:
        if any(pattern.search(text) for pattern in patterns):
            return key
    return None


def display(key_or_venue) -> str:
    """给 key 或任意 venue 字符串一个稳定的显示名。"""
    key = None
    if isinstance(key_or_venue, str) and key_or_venue in DISPLAY:
        key = key_or_venue
    else:
        key = canonical(key_or_venue)
    if key:
        return DISPLAY[key]
    text = clean_text(key_or_venue)
    return text[:60] if text else "—"


def is_preprint(venue) -> bool:
    """无 venue、或 venue 明确是预印本平台。

    注意"无 venue"与"未知 venue"是两件事：
      无 venue   = 还没正式发表（arXiv 预印本），每日增量里这是常态；
      未知 venue = 发表于某个不在白名单的场合（期刊、workshop、其他会议）。
    前者应该保留，后者在顶会模式下应该拒掉。
    """
    text = clean_text(venue)
    if not text:
        return True
    if canonical(text):
        return False
    return bool(_PREPRINT_RE.search(text))


def status(venue) -> str:
    """返回 'conference' / 'preprint' / 'unknown'。"""
    key = canonical(venue)
    if key:
        return "conference"
    return "preprint" if is_preprint(venue) else "unknown"


def known_keys() -> list:
    return [key for key, _name, _patterns in VENUES]


def check_venue_policy(paper, profile) -> tuple[bool, str | None]:
    """按 profile 的 venue 策略判断一篇论文是否放行。

    策略（profile.venue_policy）：
      off            不做 venue 过滤（旧行为，适合"我就是要整个领域"）
      plus_preprint  白名单会议 **或** 尚无 venue 的预印本（默认；日报需要前沿）
      strict         只放行白名单会议（适合"只读已中会"的检索）

    返回值第二项是给日志看的中文原因，不接受静默丢弃。
    """
    policy = getattr(profile, "venue_policy", "off")
    if policy in (None, "", "off"):
        return True, None

    venue = paper.get("venue")
    key = canonical(venue)
    wanted = getattr(profile, "venues", set())
    if not wanted:
        # 策略写了但白名单是空的：不能静默变成"什么都收"，也不能把一切都拒掉。
        # 交给 doctor 报警，这里按 off 处理。
        return True, None

    if key and key in wanted:
        return True, None
    if key and wanted:
        return False, f"会议「{display(key)}」不在白名单"

    state = status(venue)
    if state == "preprint" and policy == "plus_preprint":
        return True, None
    if state == "preprint":
        # strict 模式下预印本也拒 —— 但把原因说清楚，否则用户会以为丢了数据
        return False, "尚无会议信息（strict 模式只收已中会论文）"
    return False, f"venue「{clean_text(venue)[:40]}」无法归一到白名单会议"
