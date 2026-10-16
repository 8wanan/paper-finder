"""Crossref 适配器 —— 可选的元数据补充源。

实测结论（2026-10-16）：Crossref 可用，polite pool 下返回 200，
但**大量记录没有摘要**（实测一条 Elsevier 期刊文章 abstract 为空）。
没有摘要就无法做摘要级筛选，所以本源**默认关闭**，只在你明确需要
补齐 venue / DOI / 引用数时启用：`--sources arxiv,crossref`。

它唯一不可替代的价值：把 arXiv 预印本关联到正式发表的期刊/会议版本。
"""

from __future__ import annotations

import html
import re
import urllib.parse

from ..normalize import clean_text, norm_doi

__all__ = ["CrossrefSource"]

_SELECT = ",".join(
    [
        "DOI", "title", "issued", "abstract", "author",
        "container-title", "is-referenced-by-count", "URL", "type",
    ]
)

_TAGS = re.compile(r"<[^>]+>")


def _strip_jats(value) -> str:
    """Crossref 的 abstract 是 JATS XML 片段，需要剥标签并反转义。"""
    if not value:
        return ""
    text = _TAGS.sub(" ", str(value))
    return clean_text(html.unescape(text))


class CrossrefSource:
    name = "crossref"
    label = "Crossref"
    endpoint = "https://api.crossref.org/works"
    min_interval = 1.2

    def __init__(self, client, log=None):
        self.client = client
        self.log = log or (lambda msg: None)

    def fetch(self, query: str, limit: int = 100, from_date: str | None = None,
              page_size: int = 100):
        papers = []
        offset = 0
        while len(papers) < limit:
            params = {
                "query.bibliographic": query,
                "rows": min(page_size, 100, limit - len(papers)),
                "offset": offset,
                "select": _SELECT,
            }
            if self.client.contact_email:
                params["mailto"] = self.client.contact_email
            if from_date:
                params["filter"] = f"from-pub-date:{from_date}"

            url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
            payload = self.client.get_json(url, min_interval=self.min_interval)
            items = ((payload or {}).get("message") or {}).get("items") or []
            if not items:
                break
            papers.extend(self._parse_item(item) for item in items)
            offset += len(items)
            if len(items) < params["rows"]:
                break
        return papers

    def _parse_item(self, item: dict) -> dict:
        titles = item.get("title") or []
        title = clean_text(titles[0]) if titles else ""

        year = None
        issued = ((item.get("issued") or {}).get("date-parts") or [[]])[0]
        if issued and isinstance(issued[0], int):
            year = issued[0]

        authors = []
        for author in item.get("author") or []:
            given = clean_text(author.get("given"))
            family = clean_text(author.get("family"))
            name = clean_text(f"{given} {family}") or clean_text(author.get("name"))
            if name:
                authors.append(name)

        containers = item.get("container-title") or []
        venue = clean_text(containers[0]) if containers else None

        doi = norm_doi(item.get("DOI")) or None
        abstract = _strip_jats(item.get("abstract")) or None

        return {
            "doi": doi,
            "arxiv_id": None,
            "title": title,
            "abstract": abstract,
            "year": year,
            "published": None,
            "venue": venue,
            "authors": authors,
            "categories": [],
            "citation_count": item.get("is-referenced-by-count"),
            "oa_url": None,
            "url": clean_text(item.get("URL")) or (f"https://doi.org/{doi}" if doi else None),
            "sources": ["crossref"],
            "raw_ids": {"doi": doi} if doi else {},
        }
