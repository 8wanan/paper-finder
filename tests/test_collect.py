"""会议批量采集测试（离线）。

锁住三条行为：
  1. 一次采集会写进账本、建一次 mode=conference 的运行、并在 meta 里记 done；
  2. 幂等：第二次调用不再请求任何源（会议清单一年只变一两次，重复拉是浪费）；
  3. 会议版能覆盖预印本的 venue（先看到 arXiv、后看到会议版时，会议信息不能丢）。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.config import Profile  # noqa: E402
from papers.pipeline import collect_conferences  # noqa: E402
from papers.store import Store  # noqa: E402
from tests.helpers import cleanup, temp_dir  # noqa: E402


class FakeOpenAlex:
    name = "openalex"
    label = "fake-openalex"

    def __init__(self, papers):
        self.papers = papers
        self.calls = 0

    def fetch_venue(self, source_id, year=None, limit=1200):
        self.calls += 1
        return [dict(paper) for paper in self.papers]


def make_profile(conferences=None):
    return Profile(
        {
            "name": "test-conf",
            "display_name": "测试",
            "venues": ["ccs", "icse"],
            "venue_policy": "plus_preprint",
            "conferences": conferences
            if conferences is not None
            else [{"venue": "ccs", "year": 2025, "kind": "openalex", "source_id": "S1"}],
        }
    )


def paper(title, venue="ACM SIGSAC Conference on Computer and Communications Security (CCS)",
          year=2025, abstract="LLM fuzzing finds vulnerabilities."):
    return {
        "title": title,
        "abstract": abstract,
        "venue": venue,
        "venue_authoritative": True,
        "year": year,
        "published": None,
        "doi": None,
        "arxiv_id": None,
        "authors": [],
        "categories": [],
        "sources": ["openalex"],
        "raw_ids": {},
    }


class TestCollect(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("collect-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_collect_stores_and_marks_done(self):
        source = FakeOpenAlex([paper("A"), paper("B")])
        result = collect_conferences(self.store, make_profile(), [source], client=None)

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["new"], 2)
        self.assertEqual(self.store.get_meta("conf:ccs:2025"), "done")
        run = self.store.get_run(result["run_id"])
        self.assertEqual(run["mode"], "conference")
        self.assertEqual(run["n_new"], 2)

    def test_second_run_is_idempotent(self):
        source = FakeOpenAlex([paper("A")])
        collect_conferences(self.store, make_profile(), [source], client=None)
        calls_after_first = source.calls

        result = collect_conferences(self.store, make_profile(), [source], client=None)
        self.assertEqual(source.calls, calls_after_first, "已采集过的会议不应该再发请求")
        self.assertEqual(result["fetched"], 0)

    def test_force_recollects(self):
        source = FakeOpenAlex([paper("A")])
        collect_conferences(self.store, make_profile(), [source], client=None)
        result = collect_conferences(self.store, make_profile(), [source], client=None, force=True)
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["new"], 0, "重采到的论文不该算新增")

    def test_venue_policy_rejects_off_whitelist(self):
        source = FakeOpenAlex([paper("A"), paper("B", venue="IEEE Access")])
        result = collect_conferences(self.store, make_profile(), [source], client=None)
        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(self.store.stats()["papers"], 1)

    def test_dedupes_within_one_conference(self):
        source = FakeOpenAlex([paper("Same Title"), paper("Same Title")])
        result = collect_conferences(self.store, make_profile(), [source], client=None)
        self.assertEqual(result["fetched"], 1)

    def test_conference_venue_overwrites_preprint(self):
        """先入库的 arXiv 版没有 venue，会议版到了必须能把 venue 补上。"""
        preprint = paper("Agentic Fuzzing")
        preprint["venue"] = None
        preprint["venue_authoritative"] = False
        preprint["sources"] = ["arxiv"]
        pid, _ = self.store.upsert(preprint)

        source = FakeOpenAlex([paper("Agentic Fuzzing")])
        collect_conferences(self.store, make_profile(), [source], client=None)

        row = self.store.get_paper(pid)
        self.assertIsNotNone(row["venue"])
        self.assertIn("CCS", row["venue"])

    def test_no_conferences_configured(self):
        result = collect_conferences(self.store, make_profile([]), [], client=None)
        self.assertEqual(result["fetched"], 0)
        self.assertIsNone(result["run_id"])


if __name__ == "__main__":
    unittest.main()
