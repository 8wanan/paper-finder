"""arXiv 适配器 —— 主数据源。

为什么是主源：2026-10-16 实测，它是唯一**免费、无需 API key、且稳定可用**的源
（OpenAlex 与 Semantic Scholar 无 key 时直接 429，Crossref 大量记录缺摘要）。

代价：arXiv 要求请求间隔 >= 3 秒，所以查询多时一轮要跑几分钟。这也是为什么
census 与 search 要分开：census 一次性把领域读完，之后只跑 watch。
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET

from ..net import HttpError, RateLimited
from ..normalize import clean_text, norm_arxiv

__all__ = ["ArxivSource"]

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"


def _text(node) -> str:
    return clean_text(node.text) if node is not None and node.text else ""


class ArxivSource:
    name = "arxiv"
    label = "arXiv"
    endpoint = "https://export.arxiv.org/api/query"
    # 官方建议 >= 3 秒；留一点余量
    min_interval = 3.2

    def __init__(self, client, log=None):
        self.client = client
        self.log = log or (lambda msg: None)

    def fetch(self, query: str, limit: int = 100, sort: str = "relevance",
              page_size: int = 100, from_date: str | None = None):
        """执行一条 arXiv 查询并翻页到 limit 条。

        from_date: 'YYYY-MM-DD'，用于 watch 模式只看新论文。
        """
        papers = []
        start = 0
        while len(papers) < limit:
            batch = min(page_size, limit - len(papers))
            params = {
                "search_query": query,
                "start": start,
                "max_results": batch,
                "sortBy": "submittedDate" if sort == "date" else "relevance",
                "sortOrder": "descending",
            }
            url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
            xml = self.client.get_text(url, min_interval=self.min_interval)
            entries, total = self._parse_feed(xml)
            if not entries:
                break
            papers.extend(entries)
            start += len(entries)
            if total is not None and start >= total:
                break
            if len(entries) < batch:
                break
        return papers

    def count(self, query: str):
        """只取命中总数（用 max_results=1），用于 doctor 与回归监控。"""
        params = {"search_query": query, "start": 0, "max_results": 1}
        url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
        xml = self.client.get_text(url, min_interval=self.min_interval)
        _, total = self._parse_feed(xml)
        return total

    def fetch_by_ids(self, arxiv_ids):
        """按 arXiv ID 批量精确取论文 —— seed-sync 用它补齐经典文献。

        为什么需要这条路：按关键词检索时，2016–2018 年的奠基论文会因为
        相关度排序靠后而**取不回来**（实测 DolphinAttack、Hidden Voice Commands
        都因此缺失）。但它们是被反复引用的基线，必须保证在账本里。
        按 id_list 精确取一次请求就能全部拿到，比放宽关键词便宜得多。
        """
        ids = [norm_arxiv(i) for i in (arxiv_ids or []) if norm_arxiv(i)]
        if not ids:
            return []
        papers = []
        # arXiv 的 id_list 支持逗号分隔批量查询
        for start in range(0, len(ids), 50):
            chunk = ids[start:start + 50]
            params = {"id_list": ",".join(chunk), "max_results": len(chunk)}
            url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
            xml = self.client.get_text(url, min_interval=self.min_interval)
            entries, _ = self._parse_feed(xml)
            papers.extend(entries)
        return papers

    # ── 解析 ────────────────────────────────────────────────────────
    def _parse_feed(self, xml: str):
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise HttpError(f"arXiv 返回的 XML 无法解析：{exc}") from exc

        total_node = root.find(f"{OPENSEARCH}totalResults")
        total = None
        if total_node is not None and total_node.text and total_node.text.isdigit():
            total = int(total_node.text)

        return [self._parse_entry(entry) for entry in root.findall(f"{ATOM}entry")], total

    def _parse_entry(self, entry) -> dict:
        raw_id = _text(entry.find(f"{ATOM}id"))
        arxiv_id = norm_arxiv(raw_id)
        if not arxiv_id:
            # 有些条目 id 形如 http://arxiv.org/abs/cs/0701001v1
            arxiv_id = norm_arxiv(raw_id.rsplit("/", 1)[-1])

        published = _text(entry.find(f"{ATOM}published"))
        year = None
        if len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])

        authors = [
            _text(node.find(f"{ATOM}name"))
            for node in entry.findall(f"{ATOM}author")
        ]
        authors = [a for a in authors if a]

        categories = [
            node.get("term") for node in entry.findall(f"{ATOM}category") if node.get("term")
        ]
        primary = entry.find(f"{ARXIV_NS}primary_category")
        primary_category = primary.get("term") if primary is not None else None

        doi = _text(entry.find(f"{ARXIV_NS}doi")) or None
        journal_ref = _text(entry.find(f"{ARXIV_NS}journal_ref")) or None

        url = f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else raw_id

        return {
            "doi": doi,
            "arxiv_id": arxiv_id or None,
            "title": _text(entry.find(f"{ATOM}title")),
            "abstract": _text(entry.find(f"{ATOM}summary")) or None,
            "year": year,
            "published": published or None,
            # 纯预印本 venue 留空；正式发表过则用 journal_ref。
            # 打分时"无 venue 且有 arXiv 源" => preprint 档（0.4），刻意不为 0。
            "venue": journal_ref,
            "authors": authors,
            "categories": categories,
            "primary_category": primary_category,
            "citation_count": None,
            "oa_url": f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None,
            "url": url,
            "sources": ["arxiv"],
            "raw_ids": {"arxiv": arxiv_id} if arxiv_id else {},
        }
