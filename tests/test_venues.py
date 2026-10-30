"""会议名归一化与 venue 硬过滤测试。

这两块是"顶会日报"的判定核心：归一化错了，要么漏掉整本会议，
要么把"数据库（database）"里的 ASE 误当成 Automated Software Engineering。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers import venues  # noqa: E402


class FakeProfile:
    def __init__(self, venues_set, policy):
        self.venues = set(venues_set)
        self.venue_policy = policy


class TestCanonical(unittest.TestCase):
    def test_common_spellings(self):
        cases = {
            "ACM/IEEE International Conference on Software Engineering: Companion Proceedings (ICSE-Companion)": "icse",
            "USENIX Security Symposium 2025": "usenix",
            "usenixsecurity25": None,          # 网址 slug 不是 venue 字符串
            "Network and Distributed System Security Symposium": "ndss",
            "IEEE Symposium on Security and Privacy (S&P)": "sp",
            "ACM SIGSAC Conference on Computer and Communications Security (CCS)": "ccs",
            "IEEE European Symposium on Security and Privacy": "euros&p",
            "ESEC/FSE 2025": "fse",
            "IEEE/ACM International Conference on Automated Software Engineering (ASE)": "ase",
            "ACM SIGSOFT International Symposium on Software Testing and Analysis (ISSTA)": "issta",
            "Neural Information Processing Systems": "neurips",
            "International Conference on Machine Learning": "icml",
        }
        for text, expected in cases.items():
            self.assertEqual(venues.canonical(text), expected, text)

    def test_profile_keys_are_recognised(self):
        """profile 里写的是短 key（ccs/sp/usenix），必须能直接认。"""
        for key in ("ccs", "sp", "usenix", "ndss", "icse", "fse", "ase", "issta"):
            self.assertEqual(venues.canonical(key), key)

    def test_unknown_and_preprint(self):
        self.assertIsNone(venues.canonical("IEEE Access"))
        self.assertIsNone(venues.canonical("arXiv (Cornell University)"))
        self.assertIsNone(venues.canonical(""))
        self.assertEqual(venues.status("IEEE Access"), "unknown")
        self.assertEqual(venues.status("arXiv (Cornell University)"), "preprint")
        self.assertEqual(venues.status(None), "preprint")

    def test_short_keys_do_not_hit_random_words(self):
        """短 key 必须靠词边界，否则 ase 会命中 database、acl 会命中 oracle。"""
        self.assertIsNone(venues.canonical("Database Systems Journal"))
        self.assertIsNone(venues.canonical("Oracle Cloud Infrastructure"))

    def test_display(self):
        self.assertEqual(venues.display("ccs"), "ACM CCS")
        self.assertEqual(venues.display("USENIX Security Symposium 2025"), "USENIX Security")
        self.assertEqual(venues.display(""), "—")


class TestPolicy(unittest.TestCase):
    def test_off_accepts_everything(self):
        profile = FakeProfile(set(), "off")
        self.assertTrue(venues.check_venue_policy({"venue": "IEEE Access"}, profile)[0])

    def test_plus_preprint(self):
        profile = FakeProfile({"ccs", "sp"}, "plus_preprint")
        ok, _ = venues.check_venue_policy({"venue": "ACM CCS"}, profile)
        self.assertTrue(ok)
        ok, _ = venues.check_venue_policy({"venue": None}, profile)
        self.assertTrue(ok, "预印本要保留，否则每日增量全是空的")
        ok, reason = venues.check_venue_policy({"venue": "IEEE Access"}, profile)
        self.assertFalse(ok)
        self.assertIn("IEEE Access", reason or "")

    def test_strict_rejects_preprint(self):
        profile = FakeProfile({"ccs"}, "strict")
        ok, reason = venues.check_venue_policy({"venue": None}, profile)
        self.assertFalse(ok)
        self.assertIn("strict", reason or "")

    def test_empty_whitelist_does_not_filter_silently(self):
        """策略写了但白名单空：不能静默"什么都不收"，doctor 会报警。"""
        profile = FakeProfile(set(), "strict")
        self.assertTrue(venues.check_venue_policy({"venue": "IEEE Access"}, profile)[0])


if __name__ == "__main__":
    unittest.main()
