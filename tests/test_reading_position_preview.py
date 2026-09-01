from types import SimpleNamespace

from src.services.reading_position_preview import build_reading_position_preview


class FakeParser:
    def __init__(self, text="abcdefghijklmnopqrstuvwxyz " * 30, spine_map=None):
        self.text = text
        self.spine_map = spine_map or [{"start": 0, "end": len(self.text)}]
        self.xpath_result = None
        self.cfi_result = None
        self.xpath_calls = []
        self.cfi_calls = []

    def resolve_book_path(self, filename):
        if filename == "missing.epub":
            raise FileNotFoundError(filename)
        return filename

    def extract_text_and_map(self, _path):
        return self.text, self.spine_map

    def resolve_xpath_to_index(self, filename, xpath):
        self.xpath_calls.append((filename, xpath))
        return self.xpath_result

    def resolve_cfi_to_index(self, filename, cfi):
        self.cfi_calls.append((filename, cfi))
        return self.cfi_result


class FakeAlignment:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def get_char_for_time(self, abs_id, timestamp):
        self.calls.append((abs_id, timestamp))
        return self.result


def _book(filename="book.epub", **kwargs):
    values = {
        "abs_id": "book-1",
        "original_ebook_filename": filename,
        "ebook_filename": filename,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def _state(client="kosync", percentage=0.25, **kwargs):
    values = {
        "client_name": client,
        "percentage": percentage,
        "timestamp": None,
        "xpath": None,
        "cfi": None,
        "last_updated": 100.0,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def _heading_parser(html, text):
    return FakeParser(
        text=text,
        spine_map=[{"start": 0, "end": len(text), "content": html}],
    )


def test_xpath_is_preferred_and_marker_context_is_bounded():
    parser = FakeParser()
    parser.xpath_result = 260
    state = _state(xpath="/body/DocFragment[2]/body/p[3]/text().0")

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="KoSync:kindle", ebook_parser=parser,
        context_chars=120,
    )

    assert result["status"] == "exact"
    assert result["confidence"] == "Exact · XPath"
    assert result["source"] == "KoSync"
    assert result["percentage"] == 25.0
    assert parser.xpath_calls
    assert parser.cfi_calls == []
    assert len(result["before"]) <= 120
    assert len(result["after"]) <= 120


def test_kavita_leader_uses_exact_xpath_and_display_label():
    parser = FakeParser()
    parser.xpath_result = 260
    state = _state(
        client="kavita",
        percentage=0.42,
        xpath="/body/DocFragment[2]/body/p[3]/text().0",
    )

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="Kavita", ebook_parser=parser,
    )

    assert result["status"] == "exact"
    assert result["source"] == "Kavita"
    assert result["percentage"] == 42.0
    assert parser.xpath_calls


def test_cfi_is_used_when_xpath_is_absent():
    parser = FakeParser()
    parser.cfi_result = 150
    state = _state(cfi="epubcfi(/6/4!/4/2:3)")

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="kosync", ebook_parser=parser,
    )

    assert result["status"] == "exact"
    assert result["confidence"] == "Exact · CFI"
    assert parser.cfi_calls == [("book.epub", "epubcfi(/6/4!/4/2:3)")]


def test_audio_leader_uses_stored_alignment_instead_of_linear_percentage():
    parser = FakeParser()
    alignment = FakeAlignment(result=420)
    audio = _state(client="abs", percentage=0.10, timestamp=987.5, last_updated=10)
    newer_but_not_leader = _state(client="kosync", percentage=0.80, last_updated=99)

    result = build_reading_position_preview(
        book=_book(),
        states=[newer_but_not_leader, audio],
        last_leader="ABS",
        ebook_parser=parser,
        alignment_service=alignment,
    )

    assert result["status"] == "mapped"
    assert result["confidence"] == "Mapped · audio alignment"
    assert result["source"] == "Audiobookshelf"
    assert alignment.calls == [("book-1", 987.5)]


def test_failed_precise_locator_degrades_visibly_to_percentage_estimate():
    parser = FakeParser()
    state = _state(
        percentage=0.50,
        xpath="/body/DocFragment[99]/body/p[1].0",
        cfi="epubcfi(/6/999!/4/2:0)",
    )

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="KoSync", ebook_parser=parser,
    )

    assert result["status"] == "approximate"
    assert result["confidence"] == "Approximate · percentage"
    assert "XPath and CFI could not be resolved" in result["message"]
    assert result["before"] or result["after"]


def test_non_cfi_locator_is_not_misrepresented_as_exact_cfi():
    parser = FakeParser()
    state = _state(cfi='{"href":"chapter.xhtml","locations":{"progression":0.3}}')

    result = build_reading_position_preview(
        book=_book(), states=[state], last_leader="kosync", ebook_parser=parser,
    )

    assert result["status"] == "approximate"
    assert result["confidence"] == "Approximate · percentage"
    assert parser.cfi_calls == []


def test_missing_ebook_is_unavailable_without_trying_to_guess_text():
    parser = FakeParser()
    state = _state(percentage=0.75)

    result = build_reading_position_preview(
        book=_book("missing.epub"), states=[state], last_leader="kosync", ebook_parser=parser,
    )

    assert result["status"] == "unavailable"
    assert result["confidence"] == "Unavailable"
    assert result["before"] == ""
    assert result["after"] == ""
    assert result["percentage"] == 75.0


def test_without_session_leader_newest_state_is_used_as_display_fallback():
    parser = FakeParser()
    older = _state(client="kosync", percentage=0.1, last_updated=10)
    newer = _state(client="bookorbit", percentage=0.7, last_updated=20)

    result = build_reading_position_preview(
        book=_book(), states=[older, newer], last_leader=None, ebook_parser=parser,
    )

    assert result["source"] == "BookOrbit"
    assert result["percentage"] == 70.0
    assert result["status"] == "approximate"


def test_real_epub_headings_become_compact_paragraph_boundaries():
    text = "Before warning. 6 ZENITH / NADIR 6.1 AUFBRUCH Portia sees art."
    html = (
        "<html><body><p>Before warning.</p>"
        "<h1>6 ZENITH / NADIR</h1><h2>6.1 AUFBRUCH</h2>"
        "<p>Portia sees art.</p></body></html>"
    )
    parser = _heading_parser(html, text)
    parser.xpath_result = text.index("warning") + 3

    result = build_reading_position_preview(
        book=_book(),
        states=[_state(xpath="/body/p[1]/text().0")],
        last_leader="kosync",
        ebook_parser=parser,
        context_chars=300,
    )

    assert "\n6 ZENITH / NADIR 6.1 AUFBRUCH\n" in result["after"]
    assert "6 ZENITH / NADIR\n6.1 AUFBRUCH" not in result["after"]


def test_duplicate_heading_text_is_left_in_plain_flow_when_mapping_is_ambiguous():
    text = "Chapter One Chapter One appears again in prose."
    html = (
        "<html><body><h1>Chapter One</h1>"
        "<p>Chapter One appears again in prose.</p></body></html>"
    )
    parser = _heading_parser(html, text)
    parser.xpath_result = text.index("appears")

    result = build_reading_position_preview(
        book=_book(),
        states=[_state(xpath="/body/p[1]/text().0")],
        last_leader="kosync",
        ebook_parser=parser,
        context_chars=300,
    )

    assert "\n" not in result["before"]
    assert "\n" not in result["after"]


def test_all_caps_normal_paragraph_is_not_guessed_as_a_heading():
    # A REAL <h1> is present so the boundary mechanism is actually engaged: the
    # point of this test is that only the marked-up heading breaks, never the
    # all-caps prose beside it.  Without a real heading in the fixture nothing
    # here could ever produce a break and the test would pass vacuously.
    text = "REAL HEADING Before. THIS IS EMPHASIS NOT A HEADING. After."
    html = (
        "<html><body><h1>REAL HEADING</h1><p>Before.</p>"
        "<p>THIS IS EMPHASIS NOT A HEADING.</p><p>After.</p></body></html>"
    )
    parser = _heading_parser(html, text)
    parser.xpath_result = text.index("After")

    result = build_reading_position_preview(
        book=_book(),
        states=[_state(xpath="/body/p[3]/text().0")],
        last_leader="kosync",
        ebook_parser=parser,
        context_chars=300,
    )

    assert result["before"].startswith("REAL HEADING\n")
    assert result["before"].count("\n") == 1
    assert "\n" not in result["after"]


def test_heading_containing_marker_is_left_in_plain_flow():
    text = "Before. CHAPTER TITLE After."
    html = "<html><body><p>Before.</p><h1>CHAPTER TITLE</h1><p>After.</p></body></html>"
    parser = _heading_parser(html, text)
    parser.xpath_result = text.index("TITLE") + 2

    result = build_reading_position_preview(
        book=_book(),
        states=[_state(xpath="/body/h1/text().0")],
        last_leader="kosync",
        ebook_parser=parser,
        context_chars=300,
    )

    assert "\n" not in result["before"]
    assert "\n" not in result["after"]


def test_missing_spine_markup_keeps_existing_plain_excerpt_behavior():
    text = "Before. CHAPTER TITLE After."
    parser = FakeParser(text=text)
    parser.xpath_result = text.index("After")

    result = build_reading_position_preview(
        book=_book(),
        states=[_state(xpath="/body/p[1]/text().0")],
        last_leader="kosync",
        ebook_parser=parser,
        context_chars=300,
    )

    assert result["before"] == "Before. CHAPTER TITLE "
    assert result["after"] == "After."


def test_only_spines_near_the_marker_are_parsed_for_headings(monkeypatch):
    """Heading detection must not re-parse the whole book for a 300-char window.

    A heading only reaches the excerpt when it falls entirely inside a rendered
    segment, so parsing distant spines is pure cost: on real library EPUBs the
    unbounded scan measured 53-138 ms per on-demand preview request.
    """
    import src.services.reading_position_preview as module

    texts = []
    spine_map = []
    cursor = 0
    for n in range(12):
        body = f"Body of chapter {n}. " + "filler sentence here. " * 10
        chapter_text = f"Chapter {n} {body.strip()}"
        spine_map.append({
            "start": cursor,
            "end": cursor + len(chapter_text),
            "content": (
                f"<html><body><h1>Chapter {n}</h1>"
                f"<p>{body.strip()}</p></body></html>"
            ),
        })
        texts.append(chapter_text)
        cursor += len(chapter_text) + 1

    full_text = " ".join(texts)
    parser = FakeParser(text=full_text, spine_map=spine_map)
    parser.xpath_result = full_text.index("Body of chapter 0") + 5

    real_soup = module.BeautifulSoup
    parsed = []

    def counting_soup(markup, *args, **kwargs):
        parsed.append(str(markup))
        return real_soup(markup, *args, **kwargs)

    monkeypatch.setattr(module, "BeautifulSoup", counting_soup)

    result = build_reading_position_preview(
        book=_book(),
        states=[_state(xpath="/body/p[1]/text().0")],
        last_leader="kosync",
        ebook_parser=parser,
        context_chars=300,
    )

    # The heading still renders, so the window did not simply skip everything.
    assert result["before"].startswith("Chapter 0\n")
    # Only the spines overlapping the excerpt window may be parsed.
    assert len(parsed) <= 2, f"parsed {len(parsed)} of {len(spine_map)} spines"
    assert not any(f"<h1>Chapter {n}</h1>" in "".join(parsed) for n in range(3, 12))
