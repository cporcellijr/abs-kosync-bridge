"""Regression tests for #424 — reading time double counted on BookOrbit.

BookOrbit's own web reader logs a reading session as the user reads. BookBridge
also posts an estimated session when it notices the progress move, so the same
reading was counted twice. Captured live on 2026-08-31 against Goblin Gigs:

    id=2768  epub  dur=149  pct 11.43->14.34319   <- BookOrbit's own
    id=2769  epub  dur=737  pct 11.89->14.34      <- BookBridge's estimate

Both cover the same read and end at the same percentage; ours inflated 149s of
reading to 886s. (14.34319 carries five decimals — BookBridge rounds to two, which
is how the two are told apart.)

Whether BookOrbit has a session of its own depends on *where the reading happened*,
not on the format. Reading or listening on BookOrbit's own site logs one (the audio
session id=2770, endProgress 15.068159, was BookOrbit's); progress arriving from a
third-party app that only writes position logs nothing, which is why another book
listened to in an external player had 25 of 25 sessions written solely by BookBridge.

So the fix cannot be a leader-based skip, nor a per-format rule — either would erase
real stats for one of those two cases. BookBridge instead asks BookOrbit what it
already has, which is correct for both without needing to know which happened.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.bookorbit_client import BookOrbitClient
from src.sync_manager import SyncManager


def _sync_manager(**kw):
    kw.setdefault("sync_clients", {})
    kw.setdefault("database_service", MagicMock())
    return SyncManager(**kw)


def _book(**over):
    base = dict(
        abs_id="bookorbit:5924",
        audio_source="BookOrbit",
        audio_provider_book_id="5924",
        audio_source_id="5924",
        ebook_source="BookOrbit",
        ebook_source_id="5920",
        ebook_filename="goblin.epub",
        sync_mode="audiobook",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _leader_state():
    return SimpleNamespace(current={"pct": 0.1434, "cfi": None}, previous_pct=0.1189)


# ---------------------------------------------------------------------------
# sync_manager: skip only when BookOrbit already has the reading
# ---------------------------------------------------------------------------

def test_session_skipped_when_bookorbit_already_logged_the_reading():
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.find_overlapping_session.return_value = {"id": 2768, "durationSeconds": 149}
    sm = _sync_manager(bookorbit_client=bo)
    with patch.object(sm, "_compute_session_duration", return_value=737):
        sm._record_bookorbit_reading_session(_book(), "BookOrbit", _leader_state(), {}, 1_000_000.0)
    bo.create_reading_session.assert_not_called()


def test_session_recorded_when_bookorbit_has_nothing():
    """The audio path, and any read BookOrbit did not log itself."""
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.find_overlapping_session.return_value = None
    sm = _sync_manager(bookorbit_client=bo)
    with patch.object(sm, "_compute_session_duration", return_value=312):
        sm._record_bookorbit_reading_session(
            _book(), "BookOrbitAudio", _leader_state(), {}, 1_000_000.0
        )
    bo.create_reading_session.assert_called_once()
    assert bo.create_reading_session.call_args.kwargs["book_type"] == "AUDIOBOOK"


def test_dedupe_is_asked_about_the_span_we_are_about_to_write():
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.find_overlapping_session.return_value = None
    sm = _sync_manager(bookorbit_client=bo)
    with patch.object(sm, "_compute_session_duration", return_value=737):
        sm._record_bookorbit_reading_session(_book(), "BookOrbit", _leader_state(), {}, 1_000_000.0)
    kwargs = bo.find_overlapping_session.call_args.kwargs
    assert {int(i) for i in kwargs["book_ids"]} == {5920, 5924}
    assert kwargs["start_progress"] == 0.1189
    assert kwargs["end_progress"] == 0.1434
    assert kwargs["end_time"] == 1_000_000.0


def test_dedupe_failure_does_not_block_recording():
    """A failed lookup must not cost the user the session."""
    bo = MagicMock()
    bo.is_configured.return_value = True
    bo.find_overlapping_session.side_effect = RuntimeError("BookOrbit unreachable")
    sm = _sync_manager(bookorbit_client=bo)
    with patch.object(sm, "_compute_session_duration", return_value=737):
        sm._record_bookorbit_reading_session(_book(), "BookOrbit", _leader_state(), {}, 1_000_000.0)
    bo.create_reading_session.assert_called_once()


# ---------------------------------------------------------------------------
# BookOrbitClient.find_overlapping_session — real payload shapes
# ---------------------------------------------------------------------------

def _client(items, per_book=None):
    """items = sessions returned for every book id; per_book = {book_id: items}."""
    client = BookOrbitClient()

    def _request(method, path, *a, **kw):
        resp = MagicMock(status_code=200)
        if per_book is not None:
            bid = int(path.split("/books/")[1].split("/")[0])
            resp.json.return_value = {"items": per_book.get(bid, [])}
        else:
            resp.json.return_value = {"items": items}
        return resp

    client._make_request = MagicMock(side_effect=_request)
    return client


# The live payload that exposed the bug (Goblin Gigs, 2026-08-31).
_REAL_SESSION = {
    "id": 2768, "bookFileId": 15511, "durationSeconds": 149,
    "startedAt": "2026-08-31T17:37:46.000Z", "endedAt": "2026-08-31T17:40:16.000Z",
    "progressDelta": 2.91, "endProgress": 14.34319, "format": "epub",
}
_END_TIME = 1_788_198_057.0  # 2026-08-31T17:40:57Z — when we would have posted (41s after theirs)


def test_find_overlapping_session_matches_the_live_duplicate():
    hit = _client([_REAL_SESSION]).find_overlapping_session(
        book_ids=[5920], start_progress=0.1189, end_progress=0.1434, end_time=_END_TIME)
    assert hit is not None and hit["id"] == 2768


def test_find_overlapping_session_matches_across_formats():
    """A stretch is consumed once whether read or listened to — and BookBridge
    syncs the position from one format onto the other, so an audiobook session
    covering these percentages is this same reading."""
    audio = dict(_REAL_SESSION, id=2767, bookFileId=15515, format="m4b")
    hit = _client([audio]).find_overlapping_session(
        book_ids=[5920], start_progress=0.1189, end_progress=0.1434, end_time=_END_TIME)
    assert hit is not None and hit["id"] == 2767


def test_find_overlapping_session_searches_every_book_id():
    """Audio and ebook are separate BookOrbit books with separate session lists."""
    audio = dict(_REAL_SESSION, id=2767, bookFileId=15515, format="m4b")
    client = _client(None, per_book={5920: [], 5924: [audio]})
    hit = client.find_overlapping_session(
        book_ids=[5920, 5924], start_progress=0.1189, end_progress=0.1434, end_time=_END_TIME)
    assert hit is not None and hit["id"] == 2767


def test_find_overlapping_session_ignores_a_disjoint_range():
    assert _client([_REAL_SESSION]).find_overlapping_session(
        book_ids=[5920], start_progress=0.50, end_progress=0.60, end_time=_END_TIME) is None


def test_find_overlapping_session_ignores_a_touching_range():
    """Consecutive reading picks up exactly where the last session ended."""
    assert _client([_REAL_SESSION]).find_overlapping_session(
        book_ids=[5920], start_progress=0.1434, end_progress=0.16, end_time=_END_TIME) is None


def test_find_overlapping_session_ignores_an_old_reread():
    """Same pages, but days later — not this reading."""
    assert _client([_REAL_SESSION]).find_overlapping_session(
        book_ids=[5920], start_progress=0.1189, end_progress=0.1434,
        end_time=_END_TIME + 5 * 86400) is None


def test_find_overlapping_session_returns_none_without_book_ids():
    assert _client([_REAL_SESSION]).find_overlapping_session(
        book_ids=[None, ""], start_progress=0.1189, end_progress=0.1434,
        end_time=_END_TIME) is None


def test_find_overlapping_session_survives_a_bad_payload():
    items = [None, {"bookFileId": 15511}, dict(_REAL_SESSION, endedAt="not-a-date"),
             dict(_REAL_SESSION, endProgress="x")]
    assert _client(items).find_overlapping_session(
        book_ids=[5920], start_progress=0.1189, end_progress=0.1434, end_time=_END_TIME) is None
