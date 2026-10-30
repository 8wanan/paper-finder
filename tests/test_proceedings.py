"""会议官网 TOC 解析器测试。

用真实页面的**结构片段**做 fixture，而不是整页 1.8MB 的 HTML：
整页 fixture 会随会议改版一起烂掉，片段只锁"我们依赖的那几处结构"。

USENIX 与 ACL 的解析规则都是实测出来的（2026-10-30，各跑过一整页）：
  · USENIX 一篇论文一个 `/presentation/<slug>` 锚点；
  · ACL 的摘要折叠块 id 是 `abstract-2025--emnlp-main--1`，
    而标题锚点是 `/2025.emnlp-main.1/` —— 两者靠 `--` ↔ `.` 互相映射。
"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from papers.sources.proceedings import (  # noqa: E402
    parse_acl_volume,
    parse_usenix,
    strip_tags,
)

USENIX_HTML = """
<div class="field field-name-field-session-papers">
  <article id="node-1" class="node node-paper view-mode-schedule">
    <h2><a href="/conference/usenixsecurity25/presentation/gibson">Agentic Fuzzing at Scale</a></h2>
    <div class="content">
      <div class="field field-name-field-paper-people-text"><div class="field-items">
        <div class="field-item odd"><p>Alice Zhang and Bob Li, <em>University of X;</em>
        Carol Wu, <em>Some Lab</em></p></div></div></div>
      <div class="required-fields group-schedule-accordion"><div class="field field-name-field-paper-description-long"><p>
        We fuzz real targets with an LLM agent and report 41 CVEs.</p></div></div>
    </div>
  </article>
  <article id="node-2" class="node node-paper view-mode-schedule">
    <h2><a href="/conference/usenixsecurity25/presentation/lee">Static Analysis Meets LLMs</a></h2>
    <div class="content">
      <div class="field field-name-field-paper-people-text"><div class="field-items">
        <div class="field-item odd"><p>Dan Lee, <em>Institute Y</em></p></div></div></div>
    </div>
  </article>
</div>
"""

ACL_HTML = """
<div class="d-sm-flex align-items-stretch mb-3"><div class="d-block me-2 list-button-row">
<a class="badge" href=https://aclanthology.org/2025.emnlp-main.1.pdf>pdf</a>
<a class="badge" href=/2025.emnlp-main.1.bib>bib</a>
<a class="badge" href=#abstract-2025--emnlp-main--1>abs</a></div>
<span class=d-block><strong><a class=align-middle href=/2025.emnlp-main.1/>Agentic Test Generation for <span class=acl-fixed-case>AI</span> Security</a></strong><br>
<a href=/people/dominic-petrak/>Dominic Petrak</a>
|
<a href=/people/thy-thy-tran/>Thy Thy Tran</a></span></div>
<div class="card bg-light mb-2 mb-lg-3 collapse abstract-collapse" id=abstract-2025--emnlp-main--1>
<div class="card-body p-3 small">We study whether agents can generate tests that expose vulnerabilities.</div></div>
<div class="d-sm-flex align-items-stretch mb-3"><div class="d-block me-2 list-button-row">
<a class="badge" href=https://aclanthology.org/2025.emnlp-main.2.pdf>pdf</a></div>
<span class=d-block><strong><a class=align-middle href=/2025.emnlp-main.2/>A Paper Without An Abstract</a></strong><br>
<a href=/people/jane-doe/>Jane Doe</a></span></div>
"""


class TestUsenix(unittest.TestCase):
    def setUp(self):
        self.papers = parse_usenix(USENIX_HTML, "USENIX Security 2025", 2025)

    def test_two_papers(self):
        self.assertEqual(len(self.papers), 2)

    def test_fields(self):
        paper = self.papers[0]
        self.assertEqual(paper["title"], "Agentic Fuzzing at Scale")
        self.assertEqual(paper["venue"], "USENIX Security 2025")
        self.assertTrue(paper["venue_authoritative"])
        self.assertIn("41 CVEs", paper["abstract"])
        self.assertTrue(paper["url"].startswith("https://www.usenix.org/conference/"))
        self.assertEqual(paper["year"], 2025)

    def test_authors_do_not_include_affiliations(self):
        authors = self.papers[0]["authors"]
        self.assertIn("Alice Zhang", authors)
        self.assertIn("Bob Li", authors)
        self.assertFalse(any("University" in a for a in authors))
        self.assertFalse(any("Lab" == a for a in authors))

    def test_missing_abstract_is_none(self):
        self.assertIsNone(self.papers[1]["abstract"])


class TestAcl(unittest.TestCase):
    def setUp(self):
        self.papers = parse_acl_volume(ACL_HTML, "EMNLP 2025", 2025)

    def test_two_papers(self):
        self.assertEqual(len(self.papers), 2)

    def test_title_is_clean(self):
        """标题前面不能带 "bib abs"：那是把 bib 链接当成标题锚点的老 bug。"""
        self.assertEqual(
            self.papers[0]["title"], "Agentic Test Generation for AI Security"
        )

    def test_abstract_mapped_by_id(self):
        self.assertIn("expose vulnerabilities", self.papers[0]["abstract"])
        self.assertIsNone(self.papers[1]["abstract"])

    def test_authors_and_urls(self):
        self.assertEqual(self.papers[0]["authors"], ["Dominic Petrak", "Thy Thy Tran"])
        self.assertEqual(self.papers[0]["url"], "https://aclanthology.org/2025.emnlp-main.1/")


class TestStripTags(unittest.TestCase):
    def test_entities_and_whitespace(self):
        self.assertEqual(strip_tags("<p>a &amp; b</p>  <i>c</i>"), "a & b c")
        self.assertEqual(strip_tags(None), "")


if __name__ == "__main__":
    unittest.main()
