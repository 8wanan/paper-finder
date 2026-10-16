"""指纹、去重与摘要还原测试。

这些是"每篇论文有自己的 ID"的实现层，也是跨源合并的正确性保证。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.normalize import (  # noqa: E402
    MIN_NTITLE_LEN,
    alias_keys,
    first_author_last,
    is_same_paper,
    norm_arxiv,
    norm_doi,
    norm_title,
    pick_pid,
    reconstruct_abstract,
    title_hash,
)


class TestNormalization(unittest.TestCase):
    def test_norm_title_strips_latex_and_punctuation(self):
        self.assertEqual(
            norm_title("Audio Adversarial Examples: Targeted Attacks!"),
            "audio adversarial examples targeted attacks",
        )
        self.assertEqual(norm_title("$x^2$ and \\textbf{bold}"), "x 2 and bold")

    def test_norm_title_is_idempotent(self):
        raw = "StyleBreak: Revealing Alignment Vulnerabilities (2025)"
        once = norm_title(raw)
        self.assertEqual(norm_title(once), once)

    def test_norm_doi(self):
        self.assertEqual(norm_doi("https://doi.org/10.1145/3133956.3134052"),
                         "10.1145/3133956.3134052")
        self.assertEqual(norm_doi("DOI:10.1145/ABC"), "10.1145/abc")
        self.assertEqual(norm_doi(None), "")

    def test_norm_arxiv_strips_version_and_url(self):
        self.assertEqual(norm_arxiv("1708.09537v1"), "1708.09537")
        self.assertEqual(norm_arxiv("http://arxiv.org/abs/2405.19103v2"), "2405.19103")
        self.assertEqual(norm_arxiv("arXiv:1801.01944"), "1801.01944")
        self.assertEqual(norm_arxiv("cs/0701001v1"), "cs/0701001")

    def test_first_author_last(self):
        # 取的是**首作者**的姓：Carlini & Wagner 那篇的首作者是 Carlini
        self.assertEqual(first_author_last(["Nicholas Carlini", "David Wagner"]), "carlini")
        self.assertEqual(first_author_last([{"name": "Guoming Zhang"}]), "zhang")
        self.assertEqual(first_author_last(["Cher"]) , "cher")
        self.assertEqual(first_author_last([]), "")


class TestFingerprint(unittest.TestCase):
    def test_doi_takes_priority(self):
        paper = {"doi": "10.1145/3133956.3134052", "arxiv_id": "1708.09537",
                 "title": "DolphinAtack"}
        self.assertEqual(pick_pid(paper), "doi:10.1145/3133956.3134052")

    def test_arxiv_when_no_doi(self):
        paper = {"arxiv_id": "2405.19103v2", "title": "Voice Jailbreak"}
        self.assertEqual(pick_pid(paper), "arxiv:2405.19103")

    def test_title_hash_fallback_is_stable(self):
        a = {"title": "Some Paper", "authors": ["A B"], "year": 2024}
        b = {"title": "some  paper!", "authors": ["A B"], "year": 2024}
        self.assertEqual(pick_pid(a), pick_pid(b))
        self.assertTrue(pick_pid(a).startswith("title:"))

    def test_alias_keys_include_multiple_identities(self):
        paper = {
            "doi": "10.1/x", "arxiv_id": "2401.00001v1",
            "title": "A Reasonably Long Paper Title For Testing",
            "authors": ["Jane Doe"], "year": 2024,
            "raw_ids": {"openalex": "W123"},
        }
        keys = dict(alias_keys(paper))
        self.assertEqual(dict(alias_keys(paper))["doi"], "10.1/x")
        self.assertIn(("arxiv", "2401.00001"), alias_keys(paper))
        self.assertIn(("openalex", "W123"), alias_keys(paper))
        self.assertIn(("ntitle", norm_title(paper["title"])), alias_keys(paper))
        self.assertIn(("title", title_hash(norm_title(paper["title"]), "doe", 2024)),
                      alias_keys(paper))

    def test_short_titles_do_not_get_ntitle_alias(self):
        """短标题（如 'Editorial'）不能生成 ntitle 别名，否则会误合并。"""
        paper = {"title": "Editorial", "year": 2024}
        kinds = [k for k, _ in alias_keys(paper)]
        self.assertNotIn("ntitle", kinds)
        self.assertLess(len(norm_title("Editorial")), MIN_NTITLE_LEN)


class TestSamePaper(unittest.TestCase):
    def test_arxiv_id_wins_over_title_typo(self):
        """回归：arXiv 上 DolphinAttack 的标题被拼成 DolphinAtack。"""
        a = {"title": "DolphinAttack: Inaudible Voice Commands", "arxiv_id": "1708.09537"}
        b = {"title": "DolphinAtack: Inaudible Voice Commands", "arxiv_id": "1708.09537"}
        self.assertTrue(is_same_paper(a, b))

    def test_truncated_subtitle_still_matches(self):
        """回归：金标集里写了截断标题，Jaccard 只有 0.71，必须靠截断容错救回。"""
        full = {"title": "StyleBreak: Revealing Alignment Vulnerabilities in Large "
                         "Audio-Language Models via Style-Aware Audio Jailbreak"}
        short = {"title": "StyleBreak: Revealing Alignment Vulnerabilities in Large "
                          "Audio-Language Models"}
        self.assertTrue(is_same_paper(full, short))

    def test_short_titles_do_not_falsely_match(self):
        self.assertFalse(is_same_paper({"title": "Editorial"}, {"title": "Editorial Note"}))

    def test_different_papers_do_not_match(self):
        a = {"title": "Voice Jailbreak Attacks Against GPT-4o"}
        b = {"title": "Muting Whisper Universal Acoustic Adversarial Attack"}
        self.assertFalse(is_same_paper(a, b))


class TestAbstractReconstruction(unittest.TestCase):
    def test_reconstruct_openalex_inverted_index(self):
        """OpenAlex 的摘要是倒排索引，必须还原成文本，否则溯源摘要全为空。"""
        inverted = {
            "We": [0], "study": [1], "audio": [2], "jailbreaks": [3],
            "on": [4], "speech": [5], "models": [6],
        }
        self.assertEqual(
            reconstruct_abstract(inverted),
            "We study audio jailbreaks on speech models",
        )

    def test_reconstruct_handles_gaps_and_duplicates(self):
        inverted = {"a": [0, 2], "b": [1]}
        self.assertEqual(reconstruct_abstract(inverted), "a b a")

    def test_reconstruct_empty(self):
        self.assertEqual(reconstruct_abstract(None), "")
        self.assertEqual(reconstruct_abstract({}), "")

    def test_reconstruct_passthrough_string(self):
        self.assertEqual(reconstruct_abstract("already text"), "already text")


if __name__ == "__main__":
    unittest.main()
