"""Resolve Kobo reading-state bookmarks (KoboSpan) from a KEPUB.

A Kobo e-reader navigates by ``CurrentBookmark.Location`` — a
``{Source, Type: "KoboSpan", Value: "kobo.N.M"}`` triple — and treats
``ProgressPercent`` as display-only. Handed a percentage alone it reopens at its own
local bookmark and pushes that back over ours (#364).

Services that host their own Kobo endpoint (BookOrbit, Grimmory) build the span
themselves from the CFI we send them. Calibre-Web Automated does not: its Kobo handler
stores whatever Location it is given and never derives one. So for CWA the bridge has to
resolve the span itself, from the very KEPUB that CWA serves the device — the only copy
whose span ids are guaranteed to be the ones on the device.

Span ids restart at ``kobo.1.1`` in every spine document, so ``Source`` is what
disambiguates them and must always travel with the value.
"""

import io
import logging
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import unquote

from lxml import html

logger = logging.getLogger(__name__)

# The literal Kobo expects in Location.Type.
KOBO_LOCATION_TYPE = "KoboSpan"

_ANCHOR_RE = re.compile(r"#.*$")
_LEADING_SLASH_RE = re.compile(r"^/+")
_KOBO_SPAN_XPATH = (
    '//span[contains(concat(" ", normalize-space(@class), " "), " koboSpan ")][@id]'
)


@dataclass
class Span:
    """One koboSpan and where its text begins within its chapter."""
    span_id: str
    start: int
    length: int


@dataclass
class Chapter:
    """One spine document, its Kobo Location.Source, and its spans in reading order."""
    source_href: str
    spans: list = field(default_factory=list)
    char_len: int = 0
    start: int = 0


@dataclass
class SpanMap:
    chapters: list = field(default_factory=list)
    total_len: int = 0


def normalize_source_href(href: str, opf_dir: str = "") -> Optional[str]:
    """Return a spine href in the form a Kobo reports as ``Location.Source``.

    That is the document's full path inside the zip — the OPF's directory plus the
    OPF-relative href — percent-decoded, with any anchor stripped, backslashes
    normalized and leading slashes removed.
    """
    if not href:
        return None
    path = _ANCHOR_RE.sub("", str(href)).replace("\\", "/")
    path = unquote(path)
    if opf_dir:
        path = posixpath.normpath(posixpath.join(opf_dir, path))
    return _LEADING_SLASH_RE.sub("", path)


def build_location(source_href: str, span_id: str) -> dict:
    """Build the Kobo ``CurrentBookmark.Location`` payload."""
    return {"Source": source_href, "Type": KOBO_LOCATION_TYPE, "Value": span_id}


def _read_opf(zf: zipfile.ZipFile) -> tuple:
    """Return ``(opf_path, opf_dir)`` for the archive, or ``(None, "")``."""
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8", "replace")
        match = re.search(r'full-path="([^"]+)"', container)
        if match:
            opf_path = match.group(1)
            return opf_path, posixpath.dirname(opf_path)
    except Exception as exc:
        logger.debug("kepub: could not read container.xml: %s", exc, exc_info=True)
    for name in zf.namelist():
        if name.lower().endswith(".opf"):
            return name, posixpath.dirname(name)
    return None, ""


def _spine_hrefs(opf_xml: str) -> list:
    """Return the OPF-relative hrefs of the spine documents, in reading order."""
    manifest = dict(
        re.findall(r'<item\b[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf_xml)
    )
    # href may precede id in the attribute order; merge both spellings.
    for href, item_id in re.findall(
        r'<item\b[^>]*\bhref="([^"]+)"[^>]*\bid="([^"]+)"', opf_xml
    ):
        manifest.setdefault(item_id, href)
    idrefs = re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf_xml)
    return [manifest[i] for i in idrefs if i in manifest]


def _spans_in_document(doc_bytes: bytes) -> tuple:
    """Return ``(spans, char_len)`` for one spine document."""
    spans = []
    cursor = 0
    try:
        tree = html.fromstring(doc_bytes)
    except Exception as exc:
        logger.debug("kepub: could not parse spine document: %s", exc, exc_info=True)
        return spans, 0
    for node in tree.xpath(_KOBO_SPAN_XPATH):
        span_id = node.get("id")
        if not span_id:
            continue
        length = len(node.text_content() or "")
        spans.append(Span(span_id=span_id, start=cursor, length=length))
        cursor += length
    return spans, cursor


def build_span_map(kepub_bytes: bytes) -> Optional[SpanMap]:
    """Parse a KEPUB into its ordered chapters and koboSpans.

    Returns None when the archive carries no koboSpans at all — i.e. it is a plain
    EPUB that was never kepubified, so there is no span namespace to address.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(kepub_bytes)) as zf:
            opf_path, opf_dir = _read_opf(zf)
            if not opf_path:
                logger.debug("kepub: no OPF found in archive")
                return None
            opf_xml = zf.read(opf_path).decode("utf-8", "replace")
            names = set(zf.namelist())

            span_map = SpanMap()
            cursor = 0
            for href in _spine_hrefs(opf_xml):
                source_href = normalize_source_href(href, opf_dir)
                if not source_href:
                    continue
                entry = source_href if source_href in names else None
                if entry is None:
                    # Fall back to the undecoded join: some archives store the
                    # percent-encoded spelling as the actual entry name.
                    raw = _LEADING_SLASH_RE.sub(
                        "", posixpath.normpath(posixpath.join(opf_dir, href))
                    )
                    entry = raw if raw in names else None
                if entry is None:
                    logger.debug("kepub: spine document not in archive: %s", source_href)
                    continue
                spans, char_len = _spans_in_document(zf.read(entry))
                if not spans:
                    continue
                span_map.chapters.append(
                    Chapter(
                        source_href=source_href,
                        spans=spans,
                        char_len=char_len,
                        start=cursor,
                    )
                )
                cursor += char_len
            span_map.total_len = cursor
    except Exception as exc:
        logger.warning("⚠️ kepub: failed to build span map: %s", exc, exc_info=True)
        return None

    if not span_map.chapters:
        logger.debug("kepub: archive carries no koboSpans (not a kepub)")
        return None
    return span_map


def _match_chapter(span_map: SpanMap, href: Optional[str]) -> Optional[Chapter]:
    """Find the chapter a locator href refers to.

    The bridge's own hrefs are already full archive paths, so this is normally an
    exact hit; the suffix comparison covers a kepub whose OPF sits at a different
    depth from the EPUB the locator was built against.
    """
    if not href:
        return None
    target = normalize_source_href(href)
    if not target:
        return None
    for chapter in span_map.chapters:
        if chapter.source_href == target:
            return chapter
    for chapter in span_map.chapters:
        if chapter.source_href.endswith(target) or target.endswith(chapter.source_href):
            return chapter
    return None


def _chapter_at_percentage(span_map: SpanMap, pct: Optional[float]) -> Optional[Chapter]:
    if pct is None or not span_map.total_len:
        return None
    offset = max(0, min(int(pct * span_map.total_len), span_map.total_len - 1))
    chosen = span_map.chapters[0]
    for chapter in span_map.chapters:
        if chapter.start <= offset:
            chosen = chapter
        else:
            break
    return chosen


def _span_at_progress(chapter: Chapter, progress: Optional[float]) -> Optional[Span]:
    if not chapter.spans:
        return None
    if progress is None or chapter.char_len <= 0:
        return chapter.spans[0]
    offset = max(0.0, min(float(progress), 1.0)) * chapter.char_len
    chosen = chapter.spans[0]
    for span in chapter.spans:
        if span.start <= offset:
            chosen = span
        else:
            break
    return chosen


def progress_for_span(span_map: SpanMap, source_href: Optional[str],
                      span_id: Optional[str]) -> Optional[float]:
    """Inverse of :func:`resolve_span` — where a span sits within its chapter.

    A device reports its bookmark as a span id, which means nothing to the plain EPUB
    the bridge normalizes against (koboSpans exist only in the kepub). Converting it
    back to an in-chapter progression lets the read side place the position properly
    instead of falling back to the start of the chapter.
    """
    if span_map is None or not source_href or not span_id:
        return None
    chapter = _match_chapter(span_map, source_href)
    if chapter is None or chapter.char_len <= 0:
        return None
    for span in chapter.spans:
        if span.span_id == span_id:
            return max(0.0, min(span.start / chapter.char_len, 1.0))
    return None


def resolve_span(span_map: SpanMap, href: Optional[str] = None,
                 chapter_progress: Optional[float] = None,
                 percentage: Optional[float] = None) -> Optional[tuple]:
    """Resolve a bridge locator to ``(source_href, span_id)``.

    Prefers the locator's own chapter href plus its in-chapter progression, which is
    what the bridge already carries. Falls back to placing the whole-book percentage
    across the concatenated chapters when no href resolves.

    Returns None when nothing can be resolved — the caller must then leave the
    device's existing bookmark alone rather than write a span it is unsure of.
    """
    if span_map is None or not span_map.chapters:
        return None

    chapter = _match_chapter(span_map, href)
    progress = chapter_progress
    if chapter is None:
        chapter = _chapter_at_percentage(span_map, percentage)
        # A percentage placed across the whole book carries no in-chapter
        # progression, so derive it from where the offset lands in that chapter.
        if chapter is not None and percentage is not None and chapter.char_len:
            offset = percentage * span_map.total_len - chapter.start
            progress = max(0.0, min(offset / chapter.char_len, 1.0))
    if chapter is None:
        return None

    span = _span_at_progress(chapter, progress)
    if span is None:
        return None
    return chapter.source_href, span.span_id
