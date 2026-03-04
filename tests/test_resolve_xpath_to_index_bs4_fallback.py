import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

# Match existing tests that add project root for `src.*` imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.ebook_utils import EbookParser


def _parser_for_single_spine(html_content: str, start: int = 0, spine_index: int = 1):
    parser = EbookParser.__new__(EbookParser)
    chapter_text = BeautifulSoup(html_content, "html.parser").get_text(separator=" ", strip=True)
    full_text = ("x" * start) + chapter_text + " tail"
    spine_map = [
        {
            "spine_index": spine_index,
            "start": start,
            "end": start + len(chapter_text),
            "content": html_content,
        }
    ]
    parser.resolve_book_path = MagicMock(return_value="book.epub")
    parser.extract_text_and_map = MagicMock(return_value=(full_text, spine_map))
    return parser


def test_resolve_xpath_to_index_exact_unique_match(caplog):
    caplog.set_level(logging.DEBUG)
    html_content = "<html><body><p>Alpha unique anchor text for deterministic matching.</p></body></html>"
    parser = _parser_for_single_spine(html_content, start=25)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/text().5")

    assert index == 30
    assert any("tier=exact_unique" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_prefix_unique_fallback(caplog):
    caplog.set_level(logging.DEBUG)
    long_head = "".join(f"{i:03d}" for i in range(50))
    html_content = f"<html><body><p><span>{long_head}</span><span>tail</span></p></body></html>"
    parser = _parser_for_single_spine(html_content, start=10)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/span[2]/text().0")

    assert index == 161
    assert any("tier=exact_unique" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_normalized_unique_fallback(caplog):
    caplog.set_level(logging.DEBUG)
    html_content = "<html><body><p><span>Alpha</span><span>Beta</span><span>Gamma</span></p></body></html>"
    parser = _parser_for_single_spine(html_content, start=40)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/span[2]/text().0")

    assert index == 46
    assert any("tier=exact_unique" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_ambiguous_returns_none(caplog):
    caplog.set_level(logging.DEBUG)
    html_content = (
        "<html><body>"
        "<p><span>Alpha</span><span>Beta</span></p>"
        "<p><span>Alpha</span><span>Beta</span></p>"
        "</body></html>"
    )
    parser = _parser_for_single_spine(html_content, start=0)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/p[1]/span[2]/text().0")

    assert index is None
    assert any("returning None" in record.message for record in caplog.records)


def test_resolve_xpath_to_index_unresolved_xpath_returns_none():
    html_content = "<html><body><p>One paragraph only.</p></body></html>"
    parser = _parser_for_single_spine(html_content, start=0)

    index = parser.resolve_xpath_to_index("book.epub", "/body/DocFragment[1]/body/div[99]/text().0")

    assert index is None
