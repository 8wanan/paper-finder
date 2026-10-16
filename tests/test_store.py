"""账本测试：指纹去重、跨源合并、别名收敛、字段回填。

这是"每篇论文有自己的 ID"最核心的一层 —— 合并错了会导致论文丢失或串号。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.store import Store  # noqa: E402
from tests.helpers import cleanup, temp_dir  # noqa: E402


def paper(**overrides):
    base = {
        "title": "Audio Jailbreak Attacks on Speech Language Models",
        "abstract": "We study audio jailbreak.",
        "year": 2025,
        "authors": ["Jane Doe"],
        "categories": ["cs.CR"],
        "sources": ["arxiv"],
    }
    base.update(overrides)
    return base


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("store-")
        self.path = os.path.join(self.tmpdir, "test.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def count(self):
        return self.store.conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]


class TestUpsert(StoreCase):
    def test_new_then_duplicate(self):
        pid1, new1 = self.store.upsert(paper(arxiv_id="2501.00001"))
        pid2, new2 = self.store.upsert(paper(arxiv_id="2501.00001"))
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(pid1, pid2)
        self.assertEqual(self.count(), 1)

    def test_version_suffix_does_not_create_duplicate(self):
        self.store.upsert(paper(arxiv_id="2501.00001v1"))
        self.store.upsert(paper(arxiv_id="2501.00001v3"))
        self.assertEqual(self.count(), 1)

    def test_empty_json_columns_get_backfilled(self):
        """回归：categories 存的是 '[]'，曾被当成"非空"而永远回填不进去，S_cat 恒为 0。"""
        self.store.upsert(paper(arxiv_id="2501.00002", categories=[]))
        row = self.store.conn.execute("SELECT categories FROM papers").fetchone()
        self.assertEqual(row["categories"], "[]")
        self.store.upsert(paper(arxiv_id="2501.00002", categories=["eess.AS", "cs.CR"]))
        row = self.store.conn.execute("SELECT categories FROM papers").fetchone()
        self.assertIn("eess.AS", row["categories"])

    def test_existing_non_empty_field_not_overwritten(self):
        self.store.upsert(paper(arxiv_id="2501.00003", abstract="original abstract"))
        self.store.upsert(paper(arxiv_id="2501.00003", abstract="different abstract"))
        row = self.store.conn.execute("SELECT abstract FROM papers").fetchone()
        self.assertEqual(row["abstract"], "original abstract")

    def test_sources_are_unioned(self):
        self.store.upsert(paper(arxiv_id="2501.00004", sources=["arxiv"]))
        self.store.upsert(paper(arxiv_id="2501.00004", sources=["openalex"]))
        row = self.store.conn.execute("SELECT sources FROM papers").fetchone()
        self.assertIn("arxiv", row["sources"])
        self.assertIn("openalex", row["sources"])


class TestMergeAcrossSources(StoreCase):
    def test_preprint_and_published_version_merge(self):
        """arXiv 预印本与正式发表版 DOI 不同，必须并成一条。

        规范 pid 采用**先到先得**（已有记录赢），而不是无脑优先 DOI：
        pid 一旦分配就要保持稳定，改名会让历史引用、判定、LLM 缓存全部错位。
        真正重要的是"两种身份都能解析到同一个 pid"。
        """
        pid_arxiv, _ = self.store.upsert(
            paper(arxiv_id="2501.00005", title="Audio Jailbreak Attacks on Speech Language Models")
        )
        pid_doi, _ = self.store.upsert(
            paper(doi="10.1145/9999999.9999999",
                  title="Audio Jailbreak Attacks on Speech Language Models",
                  sources=["crossref"])
        )
        self.assertEqual(self.count(), 1)
        self.assertEqual(pid_arxiv, pid_doi)
        # 两个身份都要能解析到它
        self.assertEqual(self.store.resolve_pid("2501.00005"), pid_arxiv)
        self.assertEqual(self.store.resolve_pid("10.1145/9999999.9999999"), pid_arxiv)

    def test_doi_first_record_keeps_doi_pid(self):
        """先以 DOI 入库的论文，之后来了 arXiv 版也不改 pid。"""
        pid_doi, _ = self.store.upsert(
            paper(doi="10.1/first-seen", title="Doi First Merging Title Here")
        )
        pid_arxiv, _ = self.store.upsert(
            paper(arxiv_id="2501.00099", title="Doi First Merging Title Here")
        )
        self.assertEqual(pid_doi, pid_arxiv)
        self.assertTrue(pid_doi.startswith("doi:"))
        self.assertEqual(self.store.resolve_pid("2501.00099"), pid_doi)

    def test_merge_keeps_earliest_first_seen(self):
        self.store.upsert(paper(arxiv_id="2501.00006", title="Merging Test Paper Title Here"))
        first = self.store.conn.execute(
            "SELECT first_seen FROM papers"
        ).fetchone()["first_seen"]
        self.store.upsert(
            paper(doi="10.1/merge", title="Merging Test Paper Title Here", sources=["crossref"])
        )
        row = self.store.conn.execute("SELECT first_seen FROM papers").fetchone()
        self.assertEqual(row["first_seen"], first)

    def test_no_duplicate_alias_rows_after_merge(self):
        self.store.upsert(paper(arxiv_id="2501.00007", title="Another Merging Test Title"))
        self.store.upsert(paper(doi="10.1/m2", title="Another Merging Test Title"))
        rows = self.store.conn.execute(
            "SELECT pid, COUNT(*) AS n FROM aliases GROUP BY pid"
        ).fetchall()
        self.assertEqual(len(rows), 1, "别名应全部收敛到同一个 pid")

    def test_decisions_survive_merge(self):
        pid1, _ = self.store.upsert(paper(arxiv_id="2501.00008", title="Decision Merge Test Title"))
        self.store.set_decision(pid1, "keep", reason="important")
        pid2, _ = self.store.upsert(paper(doi="10.1/m3", title="Decision Merge Test Title"))
        self.assertEqual(pid1, pid2)
        row = self.store.conn.execute("SELECT * FROM decisions").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["decision"], "keep")

    def test_different_papers_are_not_merged(self):
        self.store.upsert(paper(arxiv_id="2501.00009", title="Voice Jailbreak Against GPT-4o"))
        self.store.upsert(paper(arxiv_id="2501.00010", title="Muting Whisper Acoustic Attack"))
        self.assertEqual(self.count(), 2)


class TestQueries(StoreCase):
    def test_resolve_pid_prefix(self):
        pid, _ = self.store.upsert(paper(arxiv_id="2501.00011"))
        self.assertEqual(self.store.resolve_pid("2501.00011"), pid)
        self.assertIsNone(self.store.resolve_pid("nope"))

    def test_query_count_and_run_records(self):
        pid, _ = self.store.upsert(paper(arxiv_id="2501.00012"))
        run_id = self.store.start_run("census", "test", ["A1"])
        self.store.record_paper_query(pid, "A1", run_id)
        self.store.record_paper_query(pid, "A2", run_id)
        self.store.record_paper_query(pid, "A2", run_id)  # 去重
        self.store.record_run_paper(run_id, pid, True)
        self.store.commit()
        self.assertEqual(self.store.query_count(pid), 2)
        run = self.store.get_run(run_id)
        self.assertEqual(run["mode"], "census")
        self.assertEqual(len(self.store.run_paper_rows(run_id)), 1)

    def test_ranked_excludes_unscored_when_asked(self):
        pid1, _ = self.store.upsert(paper(arxiv_id="2501.00013"))
        pid2, _ = self.store.upsert(paper(arxiv_id="2501.00014", title="Second Paper Title Here"))
        self.store.set_l1(pid1, 0.8, {})
        self.store.commit()
        scored = self.store.ranked(scored_only=True)
        self.assertEqual(len(scored), 1)
        self.assertEqual(scored[0]["pid"], pid1)
        self.assertEqual(self.store.unscored_count(), 1)

    def test_decisions_roundtrip(self):
        pid, _ = self.store.upsert(paper(arxiv_id="2501.00015"))
        self.store.set_decision(pid, "drop")
        self.store.set_decision(pid, "keep")
        decisions = self.store.decisions()
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[pid]["decision"], "keep")

    def test_latest_run_skips_bookkeeping_modes(self):
        """回归：brief 默认取最新运行，而 seed-sync 这类记账运行只有几篇论文，
        会让第一次使用的用户看到一份几乎空的简报，以为工具坏了。"""
        from papers.store import SUBSTANTIVE_MODES

        self.store.start_run("import", "test", [])
        self.store.start_run("census", "test", ["A1"])
        self.store.start_run("seed-sync", "test", [])
        self.store.commit()

        self.assertEqual(self.store.latest_run()["mode"], "seed-sync")
        self.assertEqual(
            self.store.latest_run(modes=SUBSTANTIVE_MODES)["mode"], "census"
        )

    def test_latest_run_returns_none_when_only_bookkeeping(self):
        from papers.store import SUBSTANTIVE_MODES

        self.store.start_run("import", "test", [])
        self.store.commit()
        self.assertIsNone(self.store.latest_run(modes=SUBSTANTIVE_MODES))

    def test_stats_shape(self):
        self.store.upsert(paper(arxiv_id="2501.00016"))
        stats = self.store.stats()
        for key in ("papers", "with_abstract", "aliases", "runs", "decisions",
                    "scored", "llm_cached", "by_year", "by_source"):
            self.assertIn(key, stats)
        self.assertEqual(stats["papers"], 1)


if __name__ == "__main__":
    unittest.main()
