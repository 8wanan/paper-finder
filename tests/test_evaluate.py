"""金标集评测测试。

评测是"唯一能证明筛选变好还是变坏"的东西，它自己出错会把用户带进沟里。
这里覆盖两个实测抓到的坑：未打分论文沉底导致的假低召回，以及标题截断导致的假"未入库"。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.config import load_profile  # noqa: E402
from papers.evaluate import evaluate, format_report, load_golden  # noqa: E402
from papers.store import Store  # noqa: E402
from tests.helpers import cleanup, temp_dir  # noqa: E402

PROFILE = load_profile("llm-speech-security", ROOT)

GOLDEN = """
version: 0.1
profile: test
positives:
  - title: "Target Paper One"
    arxiv_id: "2501.10001"
    year: 2025
  - title: "Target Paper Two With A Long Subtitular"
    year: 2025
  - title: "Never Seen Paper"
    year: 2025
negatives:
  - title: "Irrelevant Paper"
    arxiv_id: "2501.90001"
must_recall:
  - "Target Paper One"
  - "Never Seen Paper"
"""


class EvaluateCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("eval-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))
        self.golden_path = os.path.join(self.tmpdir, "golden.yaml")
        with open(self.golden_path, "w", encoding="utf-8") as handle:
            handle.write(GOLDEN)

        # 两篇正样本（其中一篇标题被截断）、一篇负样本，另加一批干扰论文
        self.pid_one, _ = self.store.upsert({
            "arxiv_id": "2501.10001",
            "title": "Target Paper One",
            "abstract": "audio jailbreak speech attack",
            "year": 2025, "categories": ["cs.CR"], "sources": ["arxiv"],
        })
        self.pid_two, _ = self.store.upsert({
            "arxiv_id": "2501.10002",
            # 金标集里写的是截断标题，账本里是完整标题
            "title": "Target Paper Two With A Long Subtitular Extension",
            "abstract": "audio jailbreak speech attack",
            "year": 2025, "categories": ["cs.CR"], "sources": ["arxiv"],
        })
        self.pid_neg, _ = self.store.upsert({
            "arxiv_id": "2501.90001",
            "title": "Irrelevant Paper",
            "abstract": "music generation",
            "year": 2025, "sources": ["arxiv"],
        })
        for index in range(6):
            pid, _ = self.store.upsert({
                "arxiv_id": f"2501.8000{index}",
                "title": f"Distractor Paper Number {index}",
                "abstract": "unrelated content",
                "year": 2025, "sources": ["arxiv"],
            })
            self.store.set_l1(pid, 0.10 + index * 0.01, {})
        self.store.set_l1(self.pid_one, 0.90, {})
        self.store.set_l1(self.pid_two, 0.80, {})
        self.store.set_l1(self.pid_neg, 0.70, {})
        self.store.commit()

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_golden_loads(self):
        golden = load_golden(self.golden_path)
        self.assertEqual(len(golden["positives"]), 3)
        self.assertEqual(len(golden["negatives"]), 1)
        self.assertEqual(len(golden["must_recall"]), 2)

    def test_recall_counts_missing_as_failure(self):
        result = evaluate(self.store, self.golden_path, profile=PROFILE)
        # 3 个正样本里有 1 个不在账本里，必须算失败而不是跳过
        self.assertEqual(result["positives_total"], 3)
        self.assertEqual(result["positives_matched"], 2)
        self.assertAlmostEqual(result["recall"][10]["value"], 2 / 3, places=3)

    def test_truncated_title_matches(self):
        """回归：金标集写截断标题时不能误判为"未入库"。

        金标集里是 "...With A Long Subtitular"，账本里是
        "...With A Long Subtitular Extension"。Jaccard 只有 0.75，
        必须靠"交集/较短一方"这条截断容错规则救回。
        """
        result = evaluate(self.store, self.golden_path, profile=PROFILE)
        titles = [item["title"] for item in result["positives"] if item["matched"]]
        self.assertIn("Target Paper Two With A Long Subtitular", titles)

    def test_must_recall_reports_failure(self):
        result = evaluate(self.store, self.golden_path, profile=PROFILE)
        by_title = {item["title"]: item for item in result["must_recall"]}
        self.assertTrue(by_title["Target Paper One"]["ok"])
        self.assertFalse(by_title["Never Seen Paper"]["matched"])

    def test_unscored_papers_excluded_from_ranking(self):
        """回归：未打分论文在 SQLite 里排序沉底，会让相关论文显示成"第 N 名"。"""
        self.store.upsert({
            "arxiv_id": "2501.70001",
            "title": "Unscored But Relevant Target Paper One Clone",
            "year": 2025, "sources": ["arxiv"],
        })
        self.store.commit()
        result = evaluate(self.store, self.golden_path, profile=PROFILE)
        self.assertEqual(result["unscored"], 1)
        # 未打分的论文不参与排名，正样本排名不受影响
        self.assertEqual(result["recall"][10]["hits"], 2)

    def test_precision_uses_labeled_subset_only(self):
        result = evaluate(self.store, self.golden_path, profile=PROFILE)
        precision = result["precision"]
        # 前 3 名是 2 正 1 负，其余是未标注的干扰项
        self.assertEqual(precision["labeled_hits"], 2)
        self.assertEqual(precision["labeled_misses"], 1)
        self.assertAlmostEqual(precision["precision_on_labeled"], 2 / 3, places=3)

    def test_seed_check_reports_missing_seeds(self):
        result = evaluate(self.store, self.golden_path, profile=PROFILE)
        self.assertTrue(result["seed_check"])
        missing = [item for item in result["seed_check"] if not item["in_ledger"]]
        # 测试账本里没有任何真实种子论文
        self.assertEqual(len(missing), len(result["seed_check"]))

    def test_report_is_renderable(self):
        result = evaluate(self.store, self.golden_path, profile=PROFILE)
        report = format_report(result)
        self.assertIn("Recall@N", report)
        self.assertIn("必读核心召回", report)
        self.assertIn("Precision@10", report)


if __name__ == "__main__":
    unittest.main()
