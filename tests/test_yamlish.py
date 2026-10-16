"""YAML 子集解析器测试。

重点覆盖一个**实测踩到的真 bug**：`_peek()` 早期只做只读预读、不前进游标，
导致"序列项内部夹注释、后面跟下一个序列项"时同一行被解析多次 ——
表现为金标集里 1 个条目被读成 5 个。下面的 test_sequence_item_with_trailing_comments
就是这个 bug 的回归测试，删掉它这个 bug 就会回来。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.yamlish import YamlSubsetError, load, loads  # noqa: E402


class TestScalars(unittest.TestCase):
    def test_types(self):
        data = loads(
            "i: 42\n"
            "f: 0.65\n"
            "b1: true\n"
            "b2: false\n"
            "n1: null\n"
            "n2: ~\n"
            "s: hello world\n"
            "q1: 'quoted'\n"
            'q2: "double"\n'
        )
        self.assertEqual(data["i"], 42)
        self.assertAlmostEqual(data["f"], 0.65)
        self.assertIs(data["b1"], True)
        self.assertIs(data["b2"], False)
        self.assertIsNone(data["n1"])
        self.assertIsNone(data["n2"])
        self.assertEqual(data["s"], "hello world")
        self.assertEqual(data["q1"], "quoted")
        self.assertEqual(data["q2"], "double")

    def test_colon_inside_quoted_value(self):
        """标题里的冒号不能被当成 key 分隔符。"""
        data = loads('title: "StyleBreak: Revealing Vulnerabilities"\n')
        self.assertEqual(data["title"], "StyleBreak: Revealing Vulnerabilities")

    def test_colon_inside_plain_value(self):
        data = loads('q: cat:eess.AS AND cat:cs.CR\n')
        self.assertEqual(data["q"], "cat:eess.AS AND cat:cs.CR")

    def test_inline_comment_stripped(self):
        data = loads('a: 1  # trailing comment\nb: "x # not a comment"\n')
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"], "x # not a comment")

    def test_version_like_string_kept_when_quoted(self):
        """arxiv_id 必须保持字符串，否则 1708.09537 会变成浮点丢精度。"""
        data = loads('arxiv_id: "1708.09537"\n')
        self.assertIsInstance(data["arxiv_id"], str)
        self.assertEqual(data["arxiv_id"], "1708.09537")


class TestStructures(unittest.TestCase):
    def test_nested_mapping_and_sequence(self):
        data = loads(
            "scoring:\n"
            "  weights:\n"
            "    - a\n"
            "    - b\n"
            "  tau: 540\n"
        )
        self.assertEqual(data["scoring"]["weights"], ["a", "b"])
        self.assertEqual(data["scoring"]["tau"], 540)

    def test_sequence_of_inline_mappings(self):
        data = loads(
            "queries:\n"
            "  - id: A1\n"
            "    q: one\n"
            "    expect: 18\n"
            "  - id: A2\n"
            "    q: two\n"
            "    expect: 17\n"
        )
        self.assertEqual(len(data["queries"]), 2)
        self.assertEqual(data["queries"][0], {"id": "A1", "q": "one", "expect": 18})
        self.assertEqual(data["queries"][1]["expect"], 17)

    def test_block_scalar_pipe(self):
        data = loads(
            "interest: |\n"
            "  line one\n"
            "  line two\n"
            "other: 1\n"
        )
        self.assertIn("line one", data["interest"])
        self.assertIn("line two", data["interest"])
        self.assertEqual(data["other"], 1)

    def test_block_scalar_keeps_hash_lines(self):
        """块标量内容里的 # 不能当注释吃掉。"""
        data = loads(
            "note: |\n"
            "  # not a comment\n"
            "  text\n"
        )
        self.assertIn("# not a comment", data["note"])
        self.assertIn("text", data["note"])

    # ── 回归测试 ───────────────────────────────────────────────────
    def test_sequence_item_with_trailing_comments(self):
        """回归：序列项内部夹注释后跟下一个序列项，不能被重复解析。

        这是实测踩到的真 bug：_peek() 只读预读不前进游标，导致
        self.lines[self.pos] 覆盖到注释行，同一个条目被读成 5 个。
        """
        data = loads(
            "positives:\n"
            "  - title: first\n"
            "    year: 2018\n"
            "    why: something\n"
            "    # a comment inside the item\n"
            "    # another comment line\n"
            "  - title: second\n"
            "    year: 2016\n"
            "  - title: third\n"
            "    year: 2022\n"
        )
        self.assertEqual(len(data["positives"]), 3)
        self.assertEqual(
            [p["title"] for p in data["positives"]], ["first", "second", "third"]
        )

    def test_comments_between_top_level_keys(self):
        data = loads(
            "a: 1\n"
            "# comment\n"
            "\n"
            "b:\n"
            "  c: 2\n"
            "# trailing comment\n"
        )
        self.assertEqual(data["a"], 1)
        self.assertEqual(data["b"]["c"], 2)

    def test_comments_before_nested_sequence(self):
        data = loads(
            "outer:\n"
            "  # comment before items\n"
            "  - x\n"
            "  - y\n"
            "after: 1\n"
        )
        self.assertEqual(data["outer"], ["x", "y"])
        self.assertEqual(data["after"], 1)


class TestRealConfigs(unittest.TestCase):
    """对仓库里真实的配置文件做断言 —— 配置写坏了要在测试里就发现。"""

    def test_profile_parses(self):
        path = os.path.join(ROOT, "profiles", "llm-speech-security.yaml")
        data = load(path)
        self.assertEqual(data["name"], "llm-speech-security")
        self.assertEqual(len(data["census"]["queries"]), 6)
        self.assertEqual(data["census"]["queries"][0]["id"], "A1")
        self.assertEqual(data["census"]["queries"][0]["expect"], 18)
        self.assertEqual(len(data["search"]["generation_probes"]), 3)
        self.assertEqual(len(data["category_probes"]["queries"]), 2)
        self.assertIn("note", data["category_probes"])
        # 术语必须拆成两条轴
        self.assertIn("threat_terms", data["scoring"])
        self.assertIn("topic_terms", data["scoring"])
        self.assertGreaterEqual(len(data["scoring"]["threat_terms"]["high"]), 10)
        self.assertGreaterEqual(len(data["scoring"]["topic_terms"]["high"]), 10)
        # 种子必须是 mapping（含 frozen_recency），不能是裸列表
        self.assertIsInstance(data["seeds"], dict)
        self.assertTrue(data["seeds"]["frozen_recency"])
        self.assertEqual(len(data["seeds"]["items"]), 10)
        # 阈值齐全
        for key in ("l1_strong", "l1_maybe", "l2_strong", "l2_maybe", "seed_score_floor"):
            self.assertIn(key, data["thresholds"])

    def test_golden_example_parses_without_duplication(self):
        """回归：金标集曾被解析出 5 个重复条目。"""
        path = os.path.join(ROOT, "tests", "golden.example.yaml")
        data = load(path)
        titles = [p["title"] for p in data["positives"]]
        self.assertEqual(len(titles), len(set(titles)), "金标集出现重复条目")
        self.assertEqual(len(titles), 9)
        self.assertEqual(len(data["must_recall"]), 8)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load(os.path.join(ROOT, "profiles", "does-not-exist.yaml"))

    def test_invalid_structure_raises(self):
        """序列与映射混用是非法 YAML，解析器必须报错而不是静默出错。"""
        with self.assertRaises(YamlSubsetError):
            loads("key:\n  - a\n  nested: 1\n")


if __name__ == "__main__":
    unittest.main()
