"""合并日报与自动简介的测试。

两件事都容易"看起来对但实际不对"：
  · 合并日报：两个方向的候选高度重叠，不去重会出现同一篇论文列两次；
    而且顺序必须跟着打分走，不能是字典/查询的偶然顺序。
  · 自动简介：LLM 会编造摘要里没有的原句，必须逐字校验后**只丢 quote**，
    不能因为一句假引用把整条简介扔掉（那样等于白花钱）。
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.config import Profile, load_profile  # noqa: E402
from papers.render import render_digest  # noqa: E402
from papers.store import Store  # noqa: E402
from papers.summarize import build_prompt, parse_response, summarize_pending  # noqa: E402
from tests.helpers import cleanup, temp_dir  # noqa: E402

PROFILE = load_profile("ai-vuln-discovery", ROOT)


class FakeProfile:
    def __init__(self):
        self.data = {}
        self.name = "fake"
        self.display_name = "假方向 · 日报"

    @property
    def thresholds(self):
        return {"strong_top": 3, "maybe_top": 3}

    @property
    def venues(self):
        return {"ccs"}

    @property
    def max_age_days(self):
        return None

    @property
    def conference_max_age_days(self):
        return None

    @property
    def interest(self):
        return "漏洞挖掘"

    def threat_term_weight_map(self):
        return {"vulnerability": 3.0}

    def topic_term_weight_map(self):
        return {"llm": 3.0}

    @property
    def core_terms(self):
        return ["vulnerability"]

    @property
    def category_weights(self):
        return {"cs.CR": 0.4}

    @property
    def recency_tau_days(self):
        return 540

    @property
    def lex_denominator(self):
        return 24.0

    @property
    def venue_scores(self):
        return {"whitelist": 1.0, "preprint": 0.6, "other": 0.2}

    @property
    def venue_whitelist(self):
        return ["ACM CCS"]

    def seed_keys(self):
        return set()

    @property
    def seeds(self):
        return []

    @property
    def seeds_frozen_recency(self):
        return True

    @property
    def negative_terms(self):
        return []

    @property
    def safety_overrides(self):
        return []


def paper(title, venue=None, abstract="We find vulnerabilities with an LLM agent."):
    return {
        "title": title,
        "abstract": abstract,
        "venue": venue,
        "year": 2026,
        "published": "2026-10-20",
        "sources": ["arxiv"],
        "categories": ["cs.CR"],
        "authors": [],
    }


class TestParseResponse(unittest.TestCase):
    def test_quote_must_be_verbatim(self):
        response = json.dumps({
            "summary": "一个简介",
            "why_relevant": "相关",
            "quote": "this sentence is not in the abstract",
        })
        parsed = parse_response(response, "We find vulnerabilities with an LLM agent.")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["summary"], "一个简介")
        self.assertIsNone(parsed["quote"], "编造的原句必须丢掉，但简介要保留")

    def test_valid_quote_kept(self):
        response = json.dumps({
            "summary": "s", "why_relevant": "w", "quote": "We find vulnerabilities",
        })
        parsed = parse_response(response, "We find vulnerabilities with an LLM agent.")
        self.assertEqual(parsed["quote"], "We find vulnerabilities")

    def test_code_fence_and_garbage(self):
        fenced = "```json\n" + json.dumps({"summary": "x"}) + "\n```"
        self.assertEqual(parse_response(fenced, "abc")["summary"], "x")
        self.assertIsNone(parse_response("not json at all", "abc"))

    def test_prompt_contains_title_and_interest(self):
        prompt = build_prompt(PROFILE, "T", "A")
        self.assertIn("T", prompt)
        self.assertIn("漏洞", prompt)


class TestSummarizePending(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("sum-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_no_key_is_a_noop(self):
        run_id = self.store.start_run("watch", "fake", [])
        self.store.upsert(paper("p1"))
        written, _, calls = summarize_pending(self.store, FakeProfile(), {}, run_id)
        self.assertEqual((written, calls), (0, 0))

    def test_skips_existing_annotation(self):
        run_id = self.store.start_run("watch", "fake", [])
        pid, _ = self.store.upsert(paper("p1"))
        self.store.set_annotation(pid, summary="已写过")
        self.store.record_run_paper(run_id, pid, True)
        written, skipped, _ = summarize_pending(
            self.store, FakeProfile(), {"LLM_API_KEY": "k", "LLM_MODEL_CHEAP": "m"}, run_id
        )
        self.assertEqual(written, 0)


class TestRenderDigest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("digest-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))
        self.run_id = self.store.start_run("watch", "fake", [])
        for index in range(5):
            pid, _ = self.store.upsert(paper(f"Paper {index}", venue="ACM CCS"))
            self.store.record_run_paper(self.run_id, pid, True)
            self.store.set_l1(pid, 0.9 - index * 0.05, {"has_core_signal": True})

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_caps_and_orders(self):
        profile = FakeProfile()
        markdown = render_digest(self.store, [(profile, self.run_id)])
        self.assertIn("Paper 0", markdown)
        self.assertLess(markdown.index("Paper 0"), markdown.index("Paper 1"))
        self.assertNotIn("Paper 4", markdown, "超过 strong_top 的不该出现")

    def test_dedupes_across_profiles(self):
        profile = FakeProfile()
        markdown = render_digest(self.store, [(profile, self.run_id), (profile, self.run_id)])
        self.assertEqual(markdown.count("Paper 0"), 1, "同一篇不能在两个方向里各列一次")

    def test_falls_back_when_summary_missing(self):
        markdown = render_digest(self.store, [(FakeProfile(), self.run_id)])
        self.assertIn("待写简介", markdown)


if __name__ == "__main__":
    unittest.main()
