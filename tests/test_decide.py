"""决策回写测试：简报 → 账本的闭环。

闭环断了，工具就学不到用户判断，所有"评分优化"都是盲调。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.config import load_profile  # noqa: E402
from papers.decide import (  # noqa: E402
    apply_decisions,
    apply_explicit,
    load_sidecar,
    parse_brief,
)
from papers.render import render_brief, short_id_for, write_sidecar  # noqa: E402
from papers.store import Store  # noqa: E402
from tests.helpers import cleanup, temp_dir  # noqa: E402

PROFILE = load_profile("llm-speech-security", ROOT)

SAMPLE_BRIEF = """# 简报

| id | 新 | 标题 | 年 | 分数 | 为什么相关 | 决策 |
|----|----|------|----|------|-----------|------|
| `2501.00001` | ✦ | [Paper One](http://x/1) | 2025 | 0.80 | 标题命中 `jailbreak` | [k] |
| `2501.00002` |  | [Paper Two](http://x/2) | 2025 | 0.60 | 标题命中 `attack` | [m] |
| `2501.00003` |  | [Paper Three](http://x/3) | 2024 | 0.50 | 标题命中 `audio` | [d] |
| `2501.00004` |  | [Paper Four](http://x/4) | 2024 | 0.40 | 仅分类加权 | [ ] |
"""


class TestParseBrief(unittest.TestCase):
    @staticmethod
    def as_map(text):
        return {short: letter for short, letter, _ in parse_brief(text)}

    def test_parses_only_marked_rows(self):
        entries = self.as_map(SAMPLE_BRIEF)
        self.assertEqual(entries["2501.00001"], "k")
        self.assertEqual(entries["2501.00002"], "m")
        self.assertEqual(entries["2501.00003"], "d")
        self.assertNotIn("2501.00004", entries)

    def test_tolerates_note_after_decision(self):
        """用户会在决策列后面写备注，必须能解析并把备注存进 reason。"""
        text = SAMPLE_BRIEF.replace("[k]", "[k] 综述必引")
        entries = parse_brief(text)
        by_id = {short: (letter, note) for short, letter, note in entries}
        self.assertEqual(by_id["2501.00001"], ("k", "综述必引"))

    def test_decision_without_note_has_none(self):
        entries = parse_brief(SAMPLE_BRIEF)
        by_id = {short: note for short, _, note in entries}
        self.assertIsNone(by_id["2501.00002"])

    def test_ignores_non_table_lines(self):
        self.assertEqual(parse_brief("# heading\n\nsome text\n"), [])

    def test_uppercase_decision_accepted(self):
        text = SAMPLE_BRIEF.replace("[k]", "[K]")
        self.assertEqual(self.as_map(text)["2501.00001"], "k")


class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmpdir = temp_dir("decide-")
        self.store = Store(os.path.join(self.tmpdir, "t.db"))
        self.pids = {}
        for index, aid in enumerate(("2501.00001", "2501.00002", "2501.00003", "2501.00004"), 1):
            pid, _ = self.store.upsert({
                "arxiv_id": aid,
                "title": f"Paper Number {index} on Audio Jailbreak",
                "abstract": "jailbreak attack on speech models",
                "year": 2025, "categories": ["cs.CR"], "sources": ["arxiv"],
            })
            self.pids[aid] = pid
        run_id = self.store.start_run("census", PROFILE.name, ["A1"])
        for index, pid in enumerate(self.pids.values(), 1):
            # 必须给 L1 分数，否则论文会全部落进"已归档"桶 ——
            # 归档区渲染成纯列表而不是表格，决策列就不存在了。
            self.store.set_l1(pid, 0.80 - index * 0.05, {"lex": 1.0})
            self.store.record_paper_query(pid, "A1", run_id)
            self.store.record_run_paper(run_id, pid, True)
        self.store.commit()
        self.run_id = run_id

    def tearDown(self):
        self.store.close()
        cleanup(self.tmpdir)

    def test_render_then_decide(self):
        markdown, id_map, stats = render_brief(
            self.store, PROFILE, run_id=self.run_id, has_llm=False
        )
        self.assertIn("决策", markdown)
        self.assertEqual(stats["candidates"], 4)

        # 用户在简报里填了判定。注意只能改**表格行**里的 `[ ]`：
        # 简报顶部的操作说明里也含 `[ ]` 字样，直接 str.replace 会改错地方。
        lines = markdown.splitlines()
        edited = 0
        for index, line in enumerate(lines):
            if line.startswith("| `") and line.rstrip().endswith("[ ] |"):
                lines[index] = line.replace("[ ]", "[k]")
                edited += 1
                break
        self.assertEqual(edited, 1, "没找到可编辑的表格行")
        filled = "\n".join(lines)

        entries = parse_brief(filled)
        self.assertTrue(entries)

        applied, unresolved = apply_decisions(self.store, entries, id_map=id_map)
        self.assertEqual(len(applied), 1)
        self.assertEqual(unresolved, [])
        decisions = self.store.decisions()
        self.assertTrue(all(row["decision"] == "keep" for row in decisions.values()))

    def test_sidecar_survives_brief_edits(self):
        """简报被手改（改标题、调行序）也不该让回写错位 —— 靠 sidecar 绑定 id。"""
        brief_path = os.path.join(self.tmpdir, "brief.md")
        _, id_map, _ = render_brief(self.store, PROFILE, run_id=self.run_id, has_llm=False)
        with open(brief_path, "w", encoding="utf-8") as handle:
            handle.write(SAMPLE_BRIEF)
        write_sidecar(brief_path, id_map)

        loaded = load_sidecar(brief_path)
        self.assertEqual(loaded, id_map)

        entries = parse_brief(SAMPLE_BRIEF)
        applied, unresolved = apply_decisions(self.store, entries, id_map=loaded)
        # 样例简报里的 id 与真实账本一致，应全部解析
        self.assertEqual(len(applied), 3)
        self.assertEqual(unresolved, [])

    def test_short_id_is_human_readable(self):
        self.assertEqual(short_id_for("arxiv:2501.00001"), "2501.00001")
        self.assertEqual(short_id_for("doi:10.1145/abc"), "10.1145/abc")
        self.assertEqual(short_id_for("title:deadbeef"), "deadbeef")

    def test_explicit_decision_rescues_archived(self):
        pid = self.pids["2501.00004"]
        resolved = apply_explicit(self.store, pid, "k", reason="被归档但我想要")
        self.assertEqual(resolved, pid)
        self.assertEqual(self.store.decisions()[pid]["decision"], "keep")

    def test_note_becomes_reason(self):
        entries = [("2501.00001", "k", "必引的奠基工作")]
        applied, _ = apply_decisions(self.store, entries, id_map={})
        self.assertEqual(len(applied), 1)
        row = self.store.decisions()[self.pids["2501.00001"]]
        self.assertEqual(row["reason"], "必引的奠基工作")

    def test_invalid_decision_letter_rejected(self):
        with self.assertRaises(ValueError):
            apply_explicit(self.store, self.pids["2501.00001"], "z")

    def test_unknown_pid_raises(self):
        with self.assertRaises(LookupError):
            apply_explicit(self.store, "does-not-exist", "k")

    def test_unresolved_ids_are_reported_not_crashed(self):
        entries = [("bogus-id-xyz", "k", None)]
        applied, unresolved = apply_decisions(self.store, entries, id_map={})
        self.assertEqual(applied, [])
        self.assertEqual(unresolved, ["bogus-id-xyz"])


if __name__ == "__main__":
    unittest.main()
