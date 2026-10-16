"""L2 精排测试：成本闸门、缓存与幻觉校验。

这里测的是"花不花钱"和"会不会编造理由"两件事，都是不能出错的。
全部离线：_call 被替换成假的，不打任何网络。
"""

import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.config import load_profile  # noqa: E402
from papers.screen import LLMScreener  # noqa: E402
from papers.store import Store  # noqa: E402
from tests.helpers import cleanup, temp_dir  # noqa: E402

PROFILE = load_profile("llm-speech-security", ROOT)

ABSTRACT = ("We present an audio jailbreak attack against large audio-language models. "
            "Our method bypasses safety alignment in five of six models.")


def make_screener(store, response=None, env=None):
    environ = {
        "LLM_API_KEY": "test-key",
        "LLM_MODEL_CHEAP": "test-model",
        **(env or {}),
    }
    screener = LLMScreener(store, PROFILE, environ)
    calls = {"n": 0}

    def fake_call(prompt):
        calls["n"] += 1
        if callable(response):
            return response(prompt, calls["n"])
        return response

    screener._call = fake_call
    if response is not None:
        screener._response_is_fake = True
    screener.calls = 0
    screener._calls_counter = calls
    return screener, calls


VALID = json.dumps({
    "threat_model": 3,
    "targets_speech_llm": 3,
    "reproducibility": 2,
    "relevance": 3,
    "reason": "把音频越狱打到 LALM 上并报告了绕过率",
    "evidence_quote": "bypasses safety alignment in five of six models",
    "tags": ["jailbreak", "lalm"],
}, ensure_ascii=False)


class TestAvailability(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("llm-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_unavailable_without_key(self):
        screener = LLMScreener(self.store, PROFILE, {})
        self.assertFalse(screener.available)
        self.assertIsNone(screener.screen({"pid": "p", "title": "t", "abstract": ABSTRACT}))

    def test_unavailable_without_model(self):
        screener = LLMScreener(self.store, PROFILE, {"LLM_API_KEY": "k"})
        self.assertFalse(screener.available)


class TestParseAndHallucinationGate(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("llm-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_valid_response_scores(self):
        screener, _ = make_screener(self.store, VALID)
        result = screener.screen({"pid": "p1", "title": "T", "abstract": ABSTRACT})
        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 11)
        self.assertIn("绕过率", result["reason"])

    def test_fabricated_quote_is_rejected(self):
        """幻觉闸门：evidence_quote 必须逐字出现在摘要里，否则判失败。"""
        bad = json.dumps({
            "threat_model": 3, "targets_speech_llm": 3,
            "reproducibility": 3, "relevance": 3,
            "reason": "看起来很相关",
            "evidence_quote": "this sentence does not exist in the abstract",
        })
        screener, calls = make_screener(self.store, bad)
        result = screener.screen({"pid": "p2", "title": "T", "abstract": ABSTRACT})
        self.assertIsNone(result)
        self.assertEqual(calls["n"], 2, "校验失败应重试一次后放弃")

    def test_empty_quote_is_allowed(self):
        ok = json.dumps({
            "threat_model": 1, "targets_speech_llm": 1,
            "reproducibility": 1, "relevance": 1,
            "reason": "x", "evidence_quote": "",
        })
        screener, _ = make_screener(self.store, ok)
        result = screener.screen({"pid": "p3", "title": "T", "abstract": ABSTRACT})
        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 4)

    def test_scores_are_clamped_to_0_3(self):
        wild = json.dumps({
            "threat_model": 99, "targets_speech_llm": -5,
            "reproducibility": "abc", "relevance": 2,
            "reason": "x", "evidence_quote": "",
        })
        screener, _ = make_screener(self.store, wild)
        result = screener.screen({"pid": "p4", "title": "T", "abstract": ABSTRACT})
        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 3 + 0 + 0 + 2)

    def test_code_fence_is_stripped(self):
        fenced = "```json\n" + VALID + "\n```"
        screener, _ = make_screener(self.store, fenced)
        result = screener.screen({"pid": "p5", "title": "T", "abstract": ABSTRACT})
        self.assertIsNotNone(result)
        self.assertEqual(result["total"], 11)

    def test_plain_text_failure_returns_none(self):
        screener, _ = make_screener(self.store, "I cannot answer that.")
        self.assertIsNone(
            screener.screen({"pid": "p6", "title": "T", "abstract": ABSTRACT})
        )

    def test_no_abstract_never_calls_llm(self):
        """无摘要绝不让 LLM 猜 —— 它会编造理由，而用户正是靠理由决定读不读。"""
        screener, calls = make_screener(self.store, VALID)
        result = screener.screen({"pid": "p7", "title": "T", "abstract": None})
        self.assertIsNone(result)
        self.assertEqual(calls["n"], 0)


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("llm-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_second_call_hits_cache(self):
        """成本保证：相同论文重跑必须命中缓存，调用次数为 0。"""
        screener, calls = make_screener(self.store, VALID)
        paper = {"pid": "p1", "title": "T", "abstract": ABSTRACT}
        screener.screen(paper)
        first_calls = calls["n"]
        self.assertEqual(first_calls, 1)

        screener2, calls2 = make_screener(self.store, VALID)
        result = screener2.screen(paper)
        self.assertIsNotNone(result)
        self.assertEqual(calls2["n"], 0, "第二次必须命中缓存，不得重复付费")
        self.assertEqual(screener2.cache_hits, 1)

    def test_cache_row_written(self):
        screener, _ = make_screener(self.store, VALID)
        screener.screen({"pid": "p1", "title": "T", "abstract": ABSTRACT})
        count = self.store.conn.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()["n"]
        self.assertEqual(count, 1)

    def test_failed_parse_is_not_cached(self):
        bad = json.dumps({"threat_model": 3, "reason": "x",
                          "evidence_quote": "fabricated quote not in abstract"})
        screener, _ = make_screener(self.store, bad)
        screener.screen({"pid": "p9", "title": "T", "abstract": ABSTRACT})
        count = self.store.conn.execute("SELECT COUNT(*) AS n FROM llm_cache").fetchone()["n"]
        self.assertEqual(count, 0, "校验失败的输出不能进缓存")


if __name__ == "__main__":
    unittest.main()
