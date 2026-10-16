"""OpenAlex 适配器 —— 可选增强源（需要 API key）。

实测警示（2026-10-16）：无 key 时 OpenAlex 返回
    429 {"error":"Rate limit exceeded","message":"Insufficient budget.
         This request has no API key, so it counts against the free daily
         budget shared by everyone on your network"}
且 `Retry-After` 可达 53575 秒（约 15 小时）。因此本适配器**只在配置了 key 时启用**。

另一个必须处理的坑：OpenAlex 的摘要不是文本，而是倒排索引
`abstract_inverted_index`，必须还原，否则本源的摘要全部为空。

免费 key：POST https://api.openalex.org/api-keys  body {"email":"...","agent":"..."}
（用真实邮箱，点邮件里的激活链接后生效；免费额度 $1/天、30 req/s）
"""

from __future__ import annotations

import re
import urllib.parse

from ..net import HttpError
from ..normalize import clean_text, norm_arxiv, norm_doi, reconstruct_abstract

__all__ = ["OpenAlexSource"]

_SELECT = ",".join(
    [
        "id", "doi", "title", "display_name", "publication_year", "publication_date",
        "authorships", "primary_location", "locations", "cited_by_count",
        "open_access", "abstract_inverted_index", "ids", "type",
    ]
)

_ARXIV_IN_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^/?#]+)", re.I)


class OpenAlexSource:
    name = "openalex"
    label = "OpenAlex"
    endpoint = "https://api.openalex.org/works"
    min_interval = 0.4  # 免费 key 允许 30 req/s，保守取 2.5 req/s

    def __init__(self, client, api_key: str, log=None):
        self.client = client
        self.api_key = api_key
        self.log = log or (lambda msg: None)

    def fetch(self, query: str, limit: int = 100, from_date: str | None = None,
              page_size: int = 100, filters: str | None = None):
        """游标翻页。

        注意：**游标翻页必须有 key**（无 key 只能拿第一页），所以本方法只在有 key 时被调用。

        凭证通过 `Authorization: Bearer` 头传递，**不放在 URL 查询参数里**：
        URL 会被网关、代理、反向代理和访问日志整条记录下来，等于把 key 写进日志。
        OpenAlex 两种方式都支持，头传更安全。
        """
        papers = []
        cursor = "*"
        while len(papers) < limit:
            params = {
                "search": query,
                "per-page": min(page_size, 200, limit - len(papers)),
                "cursor": cursor,
                "select": _SELECT,
            }
            filter_parts = []
            if from_date:
                filter_parts.append(f"from_publication_date:{from_date}")
            if filters:
                filter_parts.append(filters)
            if filter_parts:
                params["filter"] = ",".join(filter_parts)

            url = f"{self.endpoint}?{urllib.parse.urlencode(params)}"
            payload = self.client.get_json(
                url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                min_interval=self.min_interval,
            )
            results = payload.get("results") or []
            if not results:
                break
            papers.extend(self._parse_work(work) for work in results)
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
        return papers

    def _parse_work(self, work: dict) -> dict:
        doi = norm_doi(work.get("doi")) or None
        title = clean_text(work.get("title") or work.get("display_name"))

        abstract = reconstruct_abstract(work.get("abstract_inverted_index")) or None

        authors = []
        for authorship in work.get("authorships") or []:
            name = clean_text(((authorship or {}).get("author") or {}).get("display_name"))
            if name:
                authors.append(name)

        venue, arxiv_id, oa_url = None, None, None
        primary = work.get("primary_location") or {}
        source = primary.get("source") or {}
        venue = clean_text(source.get("display_name")) or None
        oa_url = clean_text((work.get("open_access") or {}).get("oa_url")) or None

        # 从所有 location 里找 arXiv ID：这是把 OpenAlex 记录与 arXiv 记录并起来的关键
        locations = list(work.get("locations") or [])
        if primary:
            locations.insert(0, primary)
        for location in locations:
            for field in ("landing_page_url", "pdf_url"):
                match = _ARXIV_IN_URL.search(clean_text((location or {}).get(field)))
                if match:
                    arxiv_id = norm_arxiv(match.group(1))
                    break
            if arxiv_id:
                break

        openalex_id = clean_text(work.get("id")).rsplit("/", 1)[-1] or None

        return {
            "doi": doi,
            "arxiv_id": arxiv_id,
            "title": title,
            "abstract": abstract,
            "year": work.get("publication_year"),
            "published": clean_text(work.get("publication_date")) or None,
            "venue": venue,
            "authors": authors,
            "categories": [],
            "citation_count": work.get("cited_by_count"),
            "oa_url": oa_url or (f"https://arxiv.org/pdf/{arxiv_id}" if arxiv_id else None),
            "url": clean_text(primary.get("landing_page_url")) or (
                f"https://doi.org/{doi}" if doi else None
            ),
            "sources": ["openalex"],
            "raw_ids": {k: v for k, v in {"openalex": openalex_id, "doi": doi}.items() if v},
        }
