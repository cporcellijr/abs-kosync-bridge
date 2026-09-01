"""Kobo span resolution from a KEPUB (see src/utils/kepub_locator.py).

A Kobo navigates by the span id in ``CurrentBookmark.Location`` and ignores
``ProgressPercent``, so these tests pin the exact shape of the bookmark we hand it —
above all ``Location.Source``, which is what disambiguates span ids that restart at
``kobo.1.1`` in every spine document.
"""

import io
import unittest
import zipfile

from src.utils.kepub_locator import (
    KOBO_LOCATION_TYPE,
    build_location,
    build_span_map,
    normalize_source_href,
    progress_for_span,
    resolve_span,
)

CONTAINER = (
    '<?xml version="1.0"?><container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="{opf}" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def _chapter_html(*spans: str) -> bytes:
    body = "".join(
        f'<p><span class="koboSpan" id="{sid}">Sentence for {sid}. </span></p>'
        for sid in spans
    )
    return f"<html><body>{body}</body></html>".encode("utf-8")


def _build_kepub(opf_dir: str, docs: list, *, spans: bool = True) -> bytes:
    """Build an in-memory kepub. ``docs`` is a list of (opf_href, zip_path, [span ids])."""
    opf_path = f"{opf_dir}/content.opf" if opf_dir else "content.opf"
    items = "".join(
        f'<item id="c{i}" href="{href}" media-type="application/xhtml+xml"/>'
        for i, (href, _zip_path, _ids) in enumerate(docs)
    )
    refs = "".join(f'<itemref idref="c{i}"/>' for i in range(len(docs)))
    opf = (
        '<?xml version="1.0"?><package version="3.0" xmlns="http://www.idpf.org/2007/opf">'
        f"<manifest>{items}</manifest><spine>{refs}</spine></package>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/container.xml", CONTAINER.format(opf=opf_path))
        zf.writestr(opf_path, opf)
        for _href, zip_path, ids in docs:
            if spans:
                zf.writestr(zip_path, _chapter_html(*ids))
            else:
                zf.writestr(
                    zip_path,
                    b"<html><body><p>Plain paragraph with no spans.</p></body></html>",
                )
    return buf.getvalue()


class TestSourceHrefNormalization(unittest.TestCase):
    """Location.Source is the spine document's full path inside the zip."""

    def test_opf_directory_is_prefixed(self):
        self.assertEqual(
            normalize_source_href("Text/part0003.xhtml", "OEBPS"),
            "OEBPS/Text/part0003.xhtml",
        )

    def test_percent_encoding_is_decoded(self):
        self.assertEqual(
            normalize_source_href("Text/chapter%202.xhtml", "OPS"),
            "OPS/Text/chapter 2.xhtml",
        )

    def test_plus_is_not_treated_as_a_space(self):
        self.assertEqual(
            normalize_source_href("Text/chapter%2B1.xhtml", "OPS"),
            "OPS/Text/chapter+1.xhtml",
        )

    def test_anchor_and_leading_slashes_are_stripped(self):
        self.assertEqual(
            normalize_source_href("/Text/ch1.xhtml#frag", ""), "Text/ch1.xhtml"
        )

    def test_backslashes_are_normalized(self):
        self.assertEqual(
            normalize_source_href("Text\\\\ch1.xhtml", "OEBPS"), "OEBPS/Text/ch1.xhtml"
        )

    def test_empty_href_returns_none(self):
        self.assertIsNone(normalize_source_href("", "OEBPS"))


class TestBuildSpanMap(unittest.TestCase):
    def test_spine_order_and_source_hrefs(self):
        kepub = _build_kepub(
            "OEBPS",
            [
                ("Text/part0000.xhtml", "OEBPS/Text/part0000.xhtml", ["kobo.1.1", "kobo.1.2"]),
                ("Text/part0001.xhtml", "OEBPS/Text/part0001.xhtml", ["kobo.1.1", "kobo.2.1"]),
            ],
        )
        span_map = build_span_map(kepub)

        self.assertEqual(len(span_map.chapters), 2)
        self.assertEqual(span_map.chapters[0].source_href, "OEBPS/Text/part0000.xhtml")
        self.assertEqual(span_map.chapters[1].source_href, "OEBPS/Text/part0001.xhtml")
        self.assertEqual(
            [s.span_id for s in span_map.chapters[0].spans], ["kobo.1.1", "kobo.1.2"]
        )
        self.assertGreater(span_map.total_len, 0)

    def test_percent_encoded_spine_href_resolves_to_its_zip_entry(self):
        kepub = _build_kepub(
            "OPS",
            [("Text/chapter%202.xhtml", "OPS/Text/chapter 2.xhtml", ["kobo.2.1"])],
        )
        span_map = build_span_map(kepub)
        self.assertEqual(span_map.chapters[0].source_href, "OPS/Text/chapter 2.xhtml")

    def test_plain_epub_without_spans_returns_none(self):
        kepub = _build_kepub(
            "OEBPS",
            [("Text/part0000.xhtml", "OEBPS/Text/part0000.xhtml", [])],
            spans=False,
        )
        self.assertIsNone(build_span_map(kepub))

    def test_garbage_bytes_return_none_rather_than_raising(self):
        self.assertIsNone(build_span_map(b"definitely not a zip"))


class TestResolveSpan(unittest.TestCase):
    def setUp(self):
        self.kepub = _build_kepub(
            "OEBPS",
            [
                ("Text/a.xhtml", "OEBPS/Text/a.xhtml", ["kobo.1.1", "kobo.2.1", "kobo.3.1"]),
                ("Text/b.xhtml", "OEBPS/Text/b.xhtml", ["kobo.1.1", "kobo.2.1", "kobo.3.1"]),
            ],
        )
        self.span_map = build_span_map(self.kepub)

    def test_source_disambiguates_identical_span_ids_across_files(self):
        """Both chapters start at kobo.1.1 — only Source tells them apart."""
        first = resolve_span(self.span_map, href="OEBPS/Text/a.xhtml", chapter_progress=0.0)
        second = resolve_span(self.span_map, href="OEBPS/Text/b.xhtml", chapter_progress=0.0)

        self.assertEqual(first, ("OEBPS/Text/a.xhtml", "kobo.1.1"))
        self.assertEqual(second, ("OEBPS/Text/b.xhtml", "kobo.1.1"))
        self.assertEqual(first[1], second[1])
        self.assertNotEqual(first[0], second[0])

    def test_chapter_progress_advances_the_span(self):
        start = resolve_span(self.span_map, href="OEBPS/Text/a.xhtml", chapter_progress=0.0)
        end = resolve_span(self.span_map, href="OEBPS/Text/a.xhtml", chapter_progress=0.99)

        self.assertEqual(start[1], "kobo.1.1")
        self.assertEqual(end[1], "kobo.3.1")

    def test_href_matches_on_suffix_when_opf_depth_differs(self):
        """The locator's EPUB may nest differently from the kepub CWA generated."""
        resolved = resolve_span(self.span_map, href="Text/b.xhtml", chapter_progress=0.0)
        self.assertEqual(resolved, ("OEBPS/Text/b.xhtml", "kobo.1.1"))

    def test_percentage_places_the_span_when_no_href_resolves(self):
        early = resolve_span(self.span_map, percentage=0.01)
        late = resolve_span(self.span_map, percentage=0.99)

        self.assertEqual(early[0], "OEBPS/Text/a.xhtml")
        self.assertEqual(late[0], "OEBPS/Text/b.xhtml")

    def test_unresolvable_input_returns_none(self):
        self.assertIsNone(resolve_span(self.span_map))
        self.assertIsNone(resolve_span(None, percentage=0.5))

    def test_progress_for_span_is_the_inverse_of_resolve(self):
        """A device reports a span id; the read side has to place it in the chapter."""
        for progress in (0.0, 0.5, 0.99):
            with self.subTest(progress=progress):
                _href, span_id = resolve_span(
                    self.span_map, href="OEBPS/Text/a.xhtml", chapter_progress=progress
                )
                placed = progress_for_span(self.span_map, "OEBPS/Text/a.xhtml", span_id)
                self.assertIsNotNone(placed)
                self.assertLessEqual(placed, progress + 0.34)

    def test_progress_for_span_rejects_an_unknown_span(self):
        self.assertIsNone(
            progress_for_span(self.span_map, "OEBPS/Text/a.xhtml", "kobo.99.9")
        )
        self.assertIsNone(progress_for_span(self.span_map, None, "kobo.1.1"))
        self.assertIsNone(progress_for_span(self.span_map, "OEBPS/Text/a.xhtml", None))

    def test_build_location_shape(self):
        location = build_location("OEBPS/Text/a.xhtml", "kobo.2.1")
        self.assertEqual(
            location,
            {"Source": "OEBPS/Text/a.xhtml", "Type": "KoboSpan", "Value": "kobo.2.1"},
        )
        self.assertEqual(KOBO_LOCATION_TYPE, "KoboSpan")


if __name__ == "__main__":
    unittest.main()
