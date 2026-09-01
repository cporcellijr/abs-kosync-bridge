"""The chapter fallback must never invent DOM structure (#420 root cause).

PR #420 hardened the KoSync write choke point against a generated XPointer that is
syntactically valid but structurally impossible for its EPUB — `body/p[1]` emitted
for a chapter whose DOM is `body/div/p`. KOReader cannot resolve such a locator, so
it opens at the start of the book and reports that near-zero position back.

The invention itself came from one hardcoded line in the parser:

    default_xpath = f"/body/DocFragment[{spine_index}]/body/p[1]/text().0"

returned whenever the chapter content failed to parse, or when no element exposed a
*direct* text node. That value reaches more than KoSync: `get_perfect_ko_xpath` feeds
`LocatorResult.perfect_ko_xpath`, which BookOrbit sends as `koreaderProgress` and
relays to KOReader as the pull position — a path PR #420's KoSync-only guard never
sees. Worse, PR #420's validator parses the same content with the same parser, so on
the parse-failure branch it returns "unknown" and deliberately lets the bad locator
through.

The fallback now anchors to the real DOM or returns None, and every caller already
treats None as "no locator".
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.ebook_utils import EbookParser

SPINE = 56


def _parser() -> EbookParser:
    return EbookParser.__new__(EbookParser)


class TestNoInventedStructure(unittest.TestCase):
    def test_unparseable_content_yields_no_locator_instead_of_body_p1(self):
        """The branch PR #420's validator cannot see: it parses the same content
        with the same parser, so it reports 'unknown' and lets the bad path ship."""
        result = _parser()._build_sentence_level_chapter_fallback_xpath("", SPINE)
        self.assertIsNone(result)

    def test_empty_chapter_yields_no_locator(self):
        html = "<html><body><div></div></body></html>"
        self.assertIsNone(_parser()._build_sentence_level_chapter_fallback_xpath(html, SPINE))

    def test_never_emits_the_hardcoded_body_p1_anchor(self):
        """Guards the exact string the reporter saw."""
        for content in ("", "<html><body></body></html>", "<html><body><div/></body></html>"):
            result = _parser()._build_sentence_level_chapter_fallback_xpath(content, SPINE)
            if result is not None:
                self.assertNotEqual(result, f"/body/DocFragment[{SPINE}]/body/p[1]/text().0")


class TestRealParentChainIsPreserved(unittest.TestCase):
    def test_direct_text_keeps_the_div_parent(self):
        html = "<html><body><div><p>Real text here.</p></div></body></html>"
        result = _parser()._build_sentence_level_chapter_fallback_xpath(html, SPINE)
        self.assertIsNotNone(result)
        self.assertIn("body/div/p", result)
        self.assertTrue(result.startswith(f"/body/DocFragment[{SPINE}]/"))

    def test_text_inside_inline_markup_still_anchors_to_the_real_block(self):
        """The reported shape: no element has direct text, so both direct-text
        passes miss it and the old code fabricated body/p[1]."""
        html = "<html><body><div><p><span>Only inline text.</span></p></div></body></html>"
        result = _parser()._build_sentence_level_chapter_fallback_xpath(html, SPINE)
        self.assertIsNotNone(result)
        self.assertIn("body/div/p", result)
        self.assertNotIn("body/p[1]", result)

    def test_sibling_index_is_preserved(self):
        html = (
            "<html><body><div>"
            "<p><span>first</span></p><p><span>second</span></p><p><span>third</span></p>"
            "</div></body></html>"
        )
        result = _parser()._build_sentence_level_chapter_fallback_xpath(html, SPINE)
        self.assertIsNotNone(result)
        self.assertIn("p[1]", result)
        self.assertIn("div", result)

    def test_anchor_is_resolvable_against_its_own_fragment(self):
        """The whole point: the emitted path must exist in the source DOM."""
        from lxml import html as lxml_html

        content = "<html><body><div><section><p><em>Deeply nested.</em></p></section></div></body></html>"
        result = _parser()._build_sentence_level_chapter_fallback_xpath(content, SPINE)
        self.assertIsNotNone(result)

        relative = result.split(f"/body/DocFragment[{SPINE}]/", 1)[1].rsplit(".0", 1)[0]
        relative = relative.replace("/text()", "")
        tree = lxml_html.fromstring(content)
        self.assertTrue(
            tree.xpath(f"./{relative}"),
            f"emitted anchor {relative!r} does not exist in its own fragment",
        )

    def test_a_body_level_block_still_resolves(self):
        html = "<html><body><p>Text straight under body.</p></body></html>"
        result = _parser()._build_sentence_level_chapter_fallback_xpath(html, SPINE)
        self.assertIsNotNone(result)
        self.assertIn("body/p", result)


if __name__ == "__main__":
    unittest.main()
