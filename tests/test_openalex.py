"""OpenAlex 适配器测试：无 key 浅翻、有 key 深翻、按会议拉全量。

这一段的核心风险是**翻页把数据翻丢**（实测踩过 off-by-one：395 条的会议
只翻一页就停，每本会议最多进来 200 篇）。所以这里用假客户端逐页校验：

  · 无 key 时不能带 Authorization 头（也不该用 cursor）；
  · 翻到 count 为止，不多不少；
  · 有 key 时用 cursor，且凭证只出现在头里（URL 里不能有 key）。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.normalize import reconstruct_abstract  # noqa: E402
from papers.sources.openalex import OpenAlexSource  # noqa: E402


def work(index, title=None):
    return {
        "id": f"https://openalex.org/W{index}",
        "doi": f"https://doi.org/10.1145/x.{index}",
        "title": title or f"Paper {index}",
        "publication_year": 2025,
        "publication_date": "2025-05-01",
        "authorships": [{"author": {"display_name": "A. Author"}}],
        "primary_location": {
            "source": {"display_name": "ACM SIGSAC Conference on Computer and Communications Security (CCS)"},
            "landing_page_url": f"https://doi.org/10.1145/x.{index}",
        },
        "locations": [],
        "cited_by_count": 1,
        "open_access": {"oa_url": None},
        "abstract_inverted_index": {"we": [0], "fuzz": [1]},
        "ids": {},
        "type": "article",
    }


class FakeClient:
    """按 page/cursor 参数返回分页结果，并记录每次请求。"""

    def __init__(self, total, per_page=200):
        self.total = total
        self.per_page = per_page
        self.urls = []
        self.headers = []

    def get_json(self, url, headers=None, min_interval=None):
        self.urls.append(url)
        self.headers.append(headers or {})
        import urllib.parse

        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        requested = int(params.get("per-page", ["200"])[0])
        if "cursor" in params:
            cursor = params["cursor"][0]
            start = 0 if cursor == "*" else int(cursor)
        else:
            start = (int(params.get("page", ["1"])[0]) - 1) * requested
        results = [work(i) for i in range(start, min(start + requested, self.total))]
        meta = {"count": self.total}
        if "cursor" in params:
            nxt = start + requested
            meta["next_cursor"] = str(nxt) if nxt < self.total else None
        return {"results": results, "meta": meta}


class TestNoKey(unittest.TestCase):
    def test_pages_until_count(self):
        client = FakeClient(total=395)
        source = OpenAlexSource(client, api_key=None)
        papers = source.fetch_venue("S4210203175", year=2025, limit=1200)

        self.assertEqual(len(papers), 395, "不能只拿第一页；实测这里曾漏掉一半")
        self.assertEqual(len(client.urls), 2)
        for headers in client.headers:
            self.assertNotIn("Authorization", headers)

    def test_no_cursor_without_key(self):
        client = FakeClient(total=10)
        OpenAlexSource(client, api_key=None).fetch_venue("S1", year=2025)
        self.assertNotIn("cursor=", client.urls[0])
        self.assertIn("page=1", client.urls[0])

    def test_filter_contains_source_and_year(self):
        client = FakeClient(total=5)
        OpenAlexSource(client, api_key=None).fetch_venue("S4306420590", year=2026)
        self.assertIn("primary_location.source.id%3AS4306420590", client.urls[0])
        self.assertIn("publication_year%3A2026", client.urls[0])

    def test_limit_caps_results(self):
        client = FakeClient(total=500)
        papers = OpenAlexSource(client, api_key=None).fetch_venue("S1", year=2025, limit=100)
        self.assertEqual(len(papers), 100)


class TestWithKey(unittest.TestCase):
    def test_uses_cursor_and_header(self):
        client = FakeClient(total=300)
        source = OpenAlexSource(client, api_key="secret-key")
        papers = source.fetch_venue("S1", year=2025)

        self.assertEqual(len(papers), 300)
        self.assertTrue(all(h.get("Authorization") == "Bearer secret-key" for h in client.headers))
        self.assertIn("cursor=", client.urls[0])
        for url in client.urls:
            self.assertNotIn("secret-key", url, "凭证不能出现在 URL 里")


class TestParsing(unittest.TestCase):
    def test_venue_is_cleaned_and_marked_authoritative(self):
        client = FakeClient(total=1)
        paper = OpenAlexSource(client, api_key=None).fetch_venue("S1", year=2025)[0]
        self.assertIn("CCS", paper["venue"])
        self.assertTrue(paper["venue_authoritative"])
        self.assertEqual(paper["abstract"], "we fuzz")

    def test_arxiv_venue_is_not_treated_as_conference(self):
        client = FakeClient(total=1)
        original = client.get_json

        def patched(url, headers=None, min_interval=None):
            payload = original(url, headers, min_interval)
            payload["results"][0]["primary_location"]["source"]["display_name"] = "arXiv (Cornell University)"
            return payload

        client.get_json = patched
        paper = OpenAlexSource(client, api_key=None).fetch_venue("S1", year=2025)[0]
        self.assertIsNone(paper["venue"], "arXiv 不是会议，必须清空 venue 才能正确归类")
        self.assertFalse(paper["venue_authoritative"])


if __name__ == "__main__":
    unittest.main()
