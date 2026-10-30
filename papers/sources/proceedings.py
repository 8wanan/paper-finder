"""会议官网 TOC 采集器 —— 补 OpenAlex 缺收的那些会议。

为什么需要单独写（实测依据，2026-10-30）：

- **USENIX Security 在 OpenAlex 里几乎不存在**：按其 source 查 2022–2025 全部 0 条。
  而官网 `usenixsecurity25/technical-sessions` 一页就能解析出 455 篇论文 + 完整摘要。
- **ACL Anthology 收录比 OpenAlex 全**，且每篇都有摘要，页面结构稳定（`.bib` + 摘要折叠块）。

代价是要解析 HTML。这里刻意用"标题锚点切块"而不是整页 DOM 解析：
只依赖标题锚点这一处结构，块内再各取所需。这样官网改版最多影响字段，不会整体失效，
也不引入 bs4 之类的第三方依赖（本仓库的零依赖约束是有意为之，见 README）。

契约与 arxiv/openalex 适配器一致：返回统一的 Paper dict；单篇解析失败跳过而不是抛异常，
免得一条坏 HTML 拖垮整轮。
"""

from __future__ import annotations

import html as html_module
import re
import urllib.parse

from ..normalize import clean_text

__all__ = ["UsenixToc", "AclAnthologyToc", "strip_tags", "parse_usenix", "parse_acl_volume"]

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(text) -> str:
    """去标签 + 还原实体 + 归一空白。"""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", str(text))
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


# ── USENIX Security ────────────────────────────────────────────────
_TITLE_ANCHOR = re.compile(
    r'<h2>\s*<a href="(?P<href>[^"]*/presentation/[^"]+)">(?P<title>.*?)</a>', re.S
)
_PEOPLE = re.compile(r"field-name-field-paper-people-text[^>]*>(.*?)</div>\s*</div>", re.S)
_DESC = re.compile(r"field-name-field-paper-description-long[^>]*>(?P<body>.*?)</div>\s*</div>", re.S)


def _usenix_authors(people_html: str) -> list:
    """从 "Cassidy Gibson and Daniel Olszewski, <em>University of Florida;</em> ..." 里取人名。

    机构名用 <em> 包着，直接删掉 <em> 段再按标点切，剩下的就是纯人名。
    名字不参与打分，只用于无 DOI/arXiv ID 时的指纹，所以宁可漏也不要把机构当成人名。
    """
    text = re.sub(r"<em>.*?</em>", "", people_html or "", flags=re.S)
    text = strip_tags(text)
    names = []
    for chunk in re.split(r"[;,]", text):
        chunk = chunk.strip(" .")
        if not chunk or len(chunk) > 60:
            continue
        if re.search(r"universit|institut|laborator|\blab\b|inc\.|college|school|centre|center", chunk, re.I):
            continue
        for part in re.split(r"\s+and\s+", chunk):
            part = part.strip()
            if 2 <= len(part.split()) <= 5 and part[:1].isupper():
                names.append(part)
    return names


def parse_usenix(page: str, venue_label: str, year: int) -> list:
    """解析 USENIX 技术议程页（一篇论文一个 presentation 锚点）。"""
    anchors = list(_TITLE_ANCHOR.finditer(page or ""))
    papers = []
    for index, anchor in enumerate(anchors):
        start = anchor.end()
        end = anchors[index + 1].start() if index + 1 < len(anchors) else min(len(page), start + 30000)
        block = page[start:end]

        title = strip_tags(anchor.group("title"))
        if not title:
            continue
        abstract_match = _DESC.search(block)
        abstract = strip_tags(abstract_match.group("body")) if abstract_match else ""
        people_match = _PEOPLE.search(block)
        authors = _usenix_authors(people_match.group(1)) if people_match else []

        href = anchor.group("href")
        url = href if href.startswith("http") else "https://www.usenix.org" + href
        papers.append(
            {
                "doi": None,
                "arxiv_id": None,
                "title": title,
                "abstract": abstract or None,
                "year": year,
                "published": None,
                "venue": venue_label,
                "venue_authoritative": True,
                "authors": authors,
                "categories": [],
                "citation_count": None,
                "oa_url": url,
                "url": url,
                "sources": ["usenix"],
                "raw_ids": {},
            }
        )
    return papers


class UsenixToc:
    """从会议官网抓整本 USENIX Security 论文清单。"""

    name = "usenix"
    label = "USENIX 官网"
    min_interval = 3.0

    def __init__(self, client, log=None):
        self.client = client
        self.log = log or (lambda msg: None)

    def fetch(self, conf: dict):
        url = conf.get("url")
        if not url:
            raise ValueError("conferences 里的 usenix 条目缺少 url")
        page = self.client.get_text(url, min_interval=self.min_interval)
        year = int(conf.get("year") or _year_from_url(url) or 0)
        label = conf.get("venue_label") or f"USENIX Security {year}"
        papers = parse_usenix(page, label, year)
        self.log(f"    · USENIX 议程页解析出 {len(papers)} 篇")
        return papers


def _year_from_url(url: str) -> int | None:
    """从 usenixsecurity25 这种 slug 里取年份 —— 官网 URL 里的两位年份是稳定的。"""
    match = re.search(r"usenixsecurity(\d{2})\b", url or "", re.I)
    if not match:
        return None
    return 2000 + int(match.group(1))


# ── ACL Anthology ──────────────────────────────────────────────────
_ACL_TITLE = re.compile(
    # 必须以 `/<pid>/` 结尾才算标题锚点。实测：只写 `[^>]*>` 会把
    # `/2025.emnlp-main.1.bib` 这种 bib 链接也吃进来，标题前面多出 "bib abs"。
    r'href=["\']?/(?P<pid>\d{4}\.[a-z0-9\-]+\.[0-9]+)/["\']?[^>]*>(?P<title>.*?)</a>\s*</strong>',
    re.S | re.I,
)
_ACL_ABS = re.compile(
    r'id=["\']?abstract-(?P<aid>[0-9A-Za-z\-]+)["\']?[^>]*>\s*<div class="card-body[^"]*">(?P<body>.*?)</div>',
    re.S,
)
_PERSON = re.compile(r'href=["\']?/people/[^"\'>]+["\']?[^>]*>(?P<name>.*?)</a>', re.S)


def _acl_abstract_map(page: str) -> dict:
    """把 `id=abstract-2025--emnlp-main--1` 还原成 `2025.emnlp-main.1` 索引的摘要。"""
    mapping = {}
    for match in _ACL_ABS.finditer(page or ""):
        key = match.group("aid").replace("--", ".")
        mapping[key] = strip_tags(match.group("body"))
    return mapping


def parse_acl_volume(page: str, venue_label: str, year: int) -> list:
    """解析 ACL Anthology 的 volume 页（一篇论文一张卡片 + 一个折叠摘要块）。"""
    abstracts = _acl_abstract_map(page)
    papers, seen = [], set()
    for match in _ACL_TITLE.finditer(page or ""):
        pid = match.group("pid")
        if pid in seen or pid.endswith(".0"):
            continue
        title = strip_tags(match.group("title"))
        if not title:
            continue
        seen.add(pid)

        # 作者紧跟标题之后、同一张卡片里，用 <br> 分隔
        tail = page[match.end(): match.end() + 4000]
        tail = tail.split("</div>")[0]
        authors = [strip_tags(m.group("name")) for m in _PERSON.finditer(tail)]
        authors = [name for name in authors if name and " " in name][:20]

        landing = f"https://aclanthology.org/{pid}/"
        papers.append(
            {
                "doi": None,
                "arxiv_id": None,
                "title": title,
                "abstract": abstracts.get(pid) or None,
                "year": year,
                "published": None,
                "venue": venue_label,
                "venue_authoritative": True,
                "authors": authors,
                "categories": [],
                "citation_count": None,
                "oa_url": f"{landing}.pdf",
                "url": landing,
                "sources": ["acl"],
                "raw_ids": {"acl": pid},
            }
        )
    return papers


class AclAnthologyToc:
    """从 ACL Anthology 的 volume 页抓整本会议论文清单（ACL/EMNLP/NAACL）。"""

    name = "acl"
    label = "ACL Anthology"
    min_interval = 3.0

    def __init__(self, client, log=None):
        self.client = client
        self.log = log or (lambda msg: None)

    def fetch(self, conf: dict):
        url = conf.get("url")
        if not url:
            raise ValueError("conferences 里的 acl 条目缺少 url")
        page = self.client.get_text(url, min_interval=self.min_interval)
        year = int(conf.get("year") or _year_from_url(url) or 0)
        label = conf.get("venue_label") or f"{conf.get('venue', '').upper()} {year}"
        papers = parse_acl_volume(page, label, year)
        self.log(f"    · ACL Anthology 解析出 {len(papers)} 篇")
        return papers
