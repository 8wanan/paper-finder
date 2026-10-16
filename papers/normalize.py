"""论文指纹与归一化 —— 去重与身份识别的基础。

设计动机（见 DESIGN.md §2.4，以及两个实测踩坑）：
  - arXiv 上 DolphinAttack 的标题被拼错成 "DolphinAtack"，纯标题相等匹配会漏检；
  - `CarelessWhisper` 连写得 0 条，`Careless Whisper` 带空格得 2 条。
所以身份识别必须走「结构化 ID 优先」，标题只作为最后手段且要归一化。

三级指纹：
  1. `doi:<norm>`      有 DOI 就用它
  2. `arxiv:<norm>`    无 DOI 但有 arXiv ID（去版本号）
  3. `title:<hash>`    都没有：sha1(归一化标题 | 首作者姓 | 年份)

额外产生一个 `ntitle:<归一化标题>` 别名，专门用于把 arXiv 预印本与正式
发表版合并。为了不误合并（例如两篇都叫 "Editorial"），只有归一化标题
长度 >= MIN_NTITLE_LEN 时才启用这个别名。
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "MIN_NTITLE_LEN",
    "norm_title",
    "norm_doi",
    "norm_arxiv",
    "first_author_last",
    "title_hash",
    "pick_pid",
    "alias_keys",
    "reconstruct_abstract",
    "clean_text",
    "is_same_paper",
    "short_id",
]

# 归一化标题短于此长度时，不生成 ntitle 别名（避免 "editorial" 之类误合并）
MIN_NTITLE_LEN = 25

_DOI_PREFIX = re.compile(r"^\s*(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_ARXIV_PREFIX = re.compile(r"^\s*(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:\s*)", re.I)
_ARXIV_VERSION = re.compile(r"v\d+$", re.I)
# 行内数学：把 $...$ 的内容**保留**成普通文本，而不是整段丢掉。
# 丢掉会损失区分度（"$x^2$" 与 "$y^2$" 会变成同一个归一化标题）。
_MATH = re.compile(r"\$([^$]*)\$")
_LATEX_CMD = re.compile(r"\\[a-zA-Z]+")
_WS = re.compile(r"\s+")


def clean_text(value) -> str:
    """把任意来源的文本压成单行、去掉多余空白。"""
    if value is None:
        return ""
    return _WS.sub(" ", str(value)).strip()


def norm_title(title) -> str:
    """归一化标题：保留数学内容、去 LaTeX 命令、转小写、非字母数字转空格。

    这个函数是**幂等**的，测试里会断言这一点。
    """
    text = clean_text(title)
    if not text:
        return ""
    text = _MATH.sub(r" \1 ", text)      # $x^2$ -> x^2（保留内容）
    text = _LATEX_CMD.sub(" ", text)     # \textbf{bold} -> bold
    text = text.lower()
    # \W 在 Unicode 下保留中日韩字符，符合预期
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    return _WS.sub(" ", text).strip()


def norm_doi(doi) -> str:
    """归一化 DOI：去 URL 前缀、转小写。"""
    text = clean_text(doi)
    if not text:
        return ""
    text = _DOI_PREFIX.sub("", text)
    return text.strip().lower().rstrip(".")


def norm_arxiv(arxiv_id) -> str:
    """归一化 arXiv ID：去 URL 前缀、去版本号 vN。"""
    text = clean_text(arxiv_id)
    if not text:
        return ""
    text = _ARXIV_PREFIX.sub("", text)
    text = re.sub(r"\.pdf$", "", text, flags=re.I)
    text = _ARXIV_VERSION.sub("", text.strip())
    return text.strip()


def first_author_last(authors) -> str:
    """取首作者姓氏（小写、去标点），用于标题指纹。"""
    if not authors:
        return ""
    first = authors[0]
    if isinstance(first, dict):
        first = first.get("name") or first.get("family") or ""
    name = clean_text(first)
    if not name:
        return ""
    parts = [p for p in re.split(r"[\s,]+", name) if p]
    if not parts:
        return ""
    last = parts[-1] if len(parts) > 1 else parts[0]
    return re.sub(r"[^a-z]", "", last.lower())


def title_hash(ntitle: str, author_last: str, year) -> str:
    """标题指纹 = sha1(归一化标题 | 首作者姓 | 年份) 的前 16 位。"""
    payload = f"{ntitle}|{author_last or ''}|{year if year is not None else ''}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def pick_pid(paper: dict) -> str:
    """按优先级选一个规范指纹。"""
    doi = norm_doi(paper.get("doi"))
    if doi:
        return f"doi:{doi}"
    arxiv_id = norm_arxiv(paper.get("arxiv_id"))
    if arxiv_id:
        return f"arxiv:{arxiv_id}"
    ntitle = norm_title(paper.get("title"))
    digest = title_hash(ntitle, first_author_last(paper.get("authors")), paper.get("year"))
    return f"title:{digest}"


def alias_keys(paper: dict) -> list:
    """列出这篇论文的所有身份别名，用于跨源合并。"""
    keys = []
    doi = norm_doi(paper.get("doi"))
    if doi:
        keys.append(("doi", doi))
    arxiv_id = norm_arxiv(paper.get("arxiv_id"))
    if arxiv_id:
        keys.append(("arxiv", arxiv_id))
    raw_ids = paper.get("raw_ids") or {}
    for source in ("s2", "openalex"):
        value = clean_text(raw_ids.get(source))
        if value:
            keys.append((source, value))
    ntitle = norm_title(paper.get("title"))
    if ntitle:
        keys.append(
            (
                "title",
                title_hash(ntitle, first_author_last(paper.get("authors")), paper.get("year")),
            )
        )
        if len(ntitle) >= MIN_NTITLE_LEN:
            keys.append(("ntitle", ntitle))
    # 去重且保持顺序
    seen = set()
    unique = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def short_id(pid: str) -> str:
    """把规范 pid 压成人类可读的短 id，供简报展示与命令行引用。

    arxiv:2405.19103       -> 2405.19103
    doi:10.1145/3133956.33 -> 10.1145/3133956.33
    title:deadbeef         -> deadbeef

    注意短 id 是 pid 的**后缀**（去掉 `arxiv:` 这类前缀），
    所以 `WHERE pid LIKE '短id%'` 这种前缀查询永远匹配不到它 ——
    解析短 id 必须按这个函数做归一化比对。
    """
    pid = clean_text(pid)
    for prefix in ("arxiv:", "doi:", "title:"):
        if pid.startswith(prefix):
            body = pid[len(prefix):]
            return body if len(body) <= 24 else body[:24]
    return pid[:24]


def reconstruct_abstract(inverted_index) -> str:
    """把 OpenAlex 的倒排索引摘要还原成纯文本。

    OpenAlex 返回的是 {"word": [pos, ...]} 结构，不是文本。不还原的话
    Tier B 的候选会全部变成"无摘要"，L2 精排直接失效。
    """
    if not inverted_index:
        return ""
    if isinstance(inverted_index, str):
        return clean_text(inverted_index)
    positions = {}
    for word, indexes in inverted_index.items():
        if indexes is None:
            continue
        for index in indexes:
            positions[index] = word
    if not positions:
        return ""
    return clean_text(" ".join(positions[i] for i in sorted(positions)))


def is_same_paper(a: dict, b: dict) -> bool:
    """判断两篇论文是否同一篇（供测试、金标集匹配与人工核对使用）。

    标题比较用两条规则，任一命中即算同一篇：

    1. **Jaccard >= 0.9** —— 容忍拼写错误、标点差异。
    2. **交集 / 较短一方 >= 0.9** —— 容忍**副标题被截断**。
       这条是实测加的：金标集里写 "StyleBreak: Revealing Alignment Vulnerabilities
       in Large Audio-Language Models"，而 arXiv 上的完整标题还有
       "via Style-Aware Audio Jailbreak"。只用 Jaccard 会算成 0.71 而误判为
       未入库。参考文献、Zotero 条目、会议列表里的标题被截断是常态。

    为防止短标题（如 "Editorial"）误合并，交集规则要求较短一方至少 5 个 token。
    """
    doi_a, doi_b = norm_doi(a.get("doi")), norm_doi(b.get("doi"))
    if doi_a and doi_b:
        return doi_a == doi_b
    arxiv_a, arxiv_b = norm_arxiv(a.get("arxiv_id")), norm_arxiv(b.get("arxiv_id"))
    if arxiv_a and arxiv_b and arxiv_a == arxiv_b:
        return True
    ntitle_a, ntitle_b = norm_title(a.get("title")), norm_title(b.get("title"))
    if not ntitle_a or not ntitle_b:
        return False
    if ntitle_a == ntitle_b:
        return True

    set_a, set_b = set(ntitle_a.split()), set(ntitle_b.split())
    if not set_a or not set_b:
        return False
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    if union and intersection / union >= 0.9:
        return True
    shorter = min(len(set_a), len(set_b))
    if shorter >= 5 and intersection / shorter >= 0.9:
        return True
    return False
