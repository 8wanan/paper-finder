"""OpenAlex 适配器 —— 会议元数据的主力源（key 可选）。

实测记录（同一台机器，两个时间点，结论相反，必须都写下来）：

  2026-10-16：无 key 直接 429，`Retry-After` 53575 秒（约 15 小时），
              配额是"同网络所有人共享"，所以当时把本源设为"仅在有 key 时启用"。
  2026-10-30：无 key 连续十余次请求全部 200，摘要、DOI、venue、年份都齐。
              说明免费额度随时段/网络波动 —— **不能当作稳定保障，但也不该整个关掉**。

因此现在：有 key 走 `Authorization: Bearer` + 游标翻页（可深翻）；
无 key 走 `page` 翻页（浅翻，够日报用），并在 429 时按"单源失败"降级，
不拖垮整轮（见 pipeline.run_queries 的处理）。

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
from ..venues import is_preprint

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

    def __init__(self, client, api_key: str | None = None, log=None, contact_email: str = ""):
        self.client = client
        self.api_key = (api_key or "").strip() or None
        self.contact_email = (contact_email or "").strip()
        self.log = log or (lambda msg: None)
        # 无 key 时降速：不给对方添堵，也降低被 429 的概率
        self.min_interval = 1.0 if not self.api_key else self.min_interval

    # ── 公共请求 ────────────────────────────────────────────────────
    def _headers(self) -> dict:
        """凭证只放头里，绝不放进 URL（URL 会被网关和访问日志整条记下来）。"""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _with_mailto(self, params: dict) -> dict:
        """带上邮箱进 polite pool。

        OpenAlex 对"带可联系邮箱"的请求给更高的容错；无 key 时这点尤其重要 ——
        实测无 key 连续拉三四本会议就会耗尽共享额度（Retry-After 可达 20 小时）。
        """
        if self.contact_email:
            params["mailto"] = self.contact_email
        return params

    def _get(self, url: str):
        return self.client.get_json(url, headers=self._headers(), min_interval=self.min_interval)

    def _select(self) -> str:
        return _SELECT

    def fetch(self, query: str, limit: int = 100, from_date: str | None = None,
              page_size: int = 100, filters: str | None = None, sort: str = "relevance"):
        """按关键词检索。

        有 key 走游标翻页（可深翻、可超过 1 万条）；
        无 key 走 page 翻页（OpenAlex 免费只给浅翻，日报够用）。

        注意 `sort` 参数只为兼容 pipeline 的统一调用契约：arXiv 用 relevance/date，
        OpenAlex 的时间排序叫 `publication_date:desc`。传了 date 就映射过去。
        """
        papers = []
        cursor = "*"
        page = 1
        while len(papers) < limit:
            params = {
                "search": query,
                "per-page": min(page_size, 200, limit - len(papers)),
                "select": self._select(),
            }
            if sort == "date":
                params["sort"] = "publication_date:desc"
            if self.api_key:
                params["cursor"] = cursor
            else:
                params["page"] = page
            filter_parts = []
            if from_date:
                filter_parts.append(f"from_publication_date:{from_date}")
            if filters:
                filter_parts.append(filters)
            if filter_parts:
                params["filter"] = ",".join(filter_parts)

            url = f"{self.endpoint}?{urllib.parse.urlencode(self._with_mailto(params))}"
            payload = self._get(url)
            results = payload.get("results") or []
            if not results:
                break
            papers.extend(self._parse_work(work) for work in results)
            if self.api_key:
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            else:
                page += 1
                # 用"已取到的条数 vs 总数"判断是否翻完。实测踩过的 off-by-one：
                # 写成 `page * per_page > count` 时，395 条的会议只翻一页就停
                # （2*200=400 > 395），于是每本会议最多只进来 200 篇。
                count = int((payload.get("meta") or {}).get("count") or 0)
                if count and len(papers) >= count:
                    break
        return papers

    def fetch_by_filters(self, filters: str, limit: int = 1200):
        """按任意 OpenAlex filter 拉一批（会议用 source id，期刊用 ISSN）。

        为什么留这个口子：期刊没有"会议年鉴"的概念，但可以按 ISSN 过滤；
        加一个通用入口，比给每个期刊再写一个适配器省事。
        """
        papers = []
        cursor = "*"
        page = 1
        per_page = 200
        while len(papers) < limit:
            params = {
                "per-page": min(per_page, limit - len(papers)),
                "filter": filters,
                "select": self._select(),
            }
            if self.api_key:
                params["cursor"] = cursor
            else:
                params["page"] = page
            url = f"{self.endpoint}?{urllib.parse.urlencode(self._with_mailto(params))}"
            payload = self._get(url)
            results = payload.get("results") or []
            if not results:
                break
            papers.extend(self._parse_work(work) for work in results)
            if self.api_key:
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            else:
                page += 1
                count = int((payload.get("meta") or {}).get("count") or 0)
                if count and len(papers) >= count:
                    break
        return papers

    def fetch_venue(self, source_id: str, year: int | None = None,
                    from_date: str | None = None, limit: int = 1200):
        """按会议（OpenAlex source id）拉整本 proceedings，**不需要关键词**。

        这是"顶会全量"的正确入口：搜索式检索永远只能拿到命中的子集，
        而会议的正式论文清单是有限且确定的，直接按 source 过滤才谈得上完整。

        source_id 形如 `S4393917527`（ACM CCS）。venue 白名单由 profile 的
        conferences 段维护，不在这里硬编码。
        """
        filters = [f"primary_location.source.id:{source_id}"]
        if year is not None:
            filters.append(f"publication_year:{int(year)}")

        papers = []
        cursor = "*"
        page = 1
        per_page = 200
        while len(papers) < limit:
            params = {
                "per-page": min(per_page, limit - len(papers)),
                "filter": ",".join(filters),
                "select": self._select(),
            }
            if self.api_key:
                params["cursor"] = cursor
            else:
                params["page"] = page
            if from_date:
                params["filter"] = params["filter"] + f",from_publication_date:{from_date}"
            url = f"{self.endpoint}?{urllib.parse.urlencode(self._with_mailto(params))}"
            payload = self._get(url)
            results = payload.get("results") or []
            if not results:
                break
            papers.extend(self._parse_work(work) for work in results)
            if self.api_key:
                cursor = (payload.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
            else:
                page += 1
                count = int((payload.get("meta") or {}).get("count") or 0)
                if count and len(papers) >= count:
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

        # OpenAlex 会把 arXiv 自身当成一个"来源"，于是预印本的 venue 字段变成
        # "arXiv (Cornell University)"。那不是会议，留在库里会污染简报的会议列，
        # 所以清空 —— 让它在 venue 口径里继续算"尚无会议信息"。
        if venue and is_preprint(venue):
            venue = None

        return {
            "doi": doi,
            "arxiv_id": arxiv_id,
            "title": title,
            "abstract": abstract,
            "year": work.get("publication_year"),
            "published": clean_text(work.get("publication_date")) or None,
            "venue": venue,
            # 会议元数据是权威的：允许它在合并时覆盖掉空 venue 或预印本标注，
            # 否则"先看到 arXiv 版、后看到会议版"时会议信息永远写不进去。
            "venue_authoritative": bool(venue),
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
