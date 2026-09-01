"""Regression tests for #364 — a Kobo reverting the position BookBridge synced.

Two defects, both reproduced here:

* The cross-client locator fell back to a bare percentage whenever text matching
  missed, and every Kobo-backed service (BookOrbit, Grimmory, CWA) needs a CFI to
  derive the KoboSpan the device actually navigates by. Handed a percentage they
  store a number the device ignores, so it reopens at its own bookmark and pushes it
  back — which is exactly why reporters saw this only with an audiobook linked.
* The CWA writer replaced the device's locator with empty strings, wiping the span
  instead of leaving it alone when it had nothing better to offer.
"""

import os
import unittest
from unittest.mock import MagicMock

from src.sync_clients.sync_client_interface import (
    LocatorResult,
    UpdateProgressRequest,
)
from src.sync_manager import _CFI_DEPENDENT_CLIENTS, SyncManager


def _build_manager(tmp_path):
    db = MagicMock()
    db.get_books_by_status.return_value = []
    return SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=MagicMock(),
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=None,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )


def _arm_hydration(manager, *, total_len=10000, cfi="epubcfi(/6/8!/4/2/6:0)",
                   href="OEBPS/Text/part0007.xhtml", chapter_progress=0.42,
                   roundtrip=None):
    """Make the parser resolve a char offset to a real, round-tripping locator."""
    manager._get_cached_ebook_text = MagicMock(return_value=("x" * total_len, total_len))
    manager.ebook_parser.get_locator_from_char_offset = MagicMock(
        return_value=LocatorResult(
            percentage=0.5, cfi=cfi, href=href, chapter_progress=chapter_progress
        )
    )
    manager.ebook_parser.resolve_cfi_to_index = MagicMock(
        return_value=int(total_len * 0.5) if roundtrip is None else roundtrip
    )


class TestPercentageOnlyLocatorIsHydrated(unittest.TestCase):
    """Fix 1 — the root cause. A percentage alone is unusable downstream."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.manager = _build_manager(self.tmp)
        self.formatter = lambda v: f"{v:.1%}"

    def test_percentage_only_locator_gains_a_cfi(self):
        _arm_hydration(self.manager)
        bare = LocatorResult(percentage=0.5)
        self.assertIsNone(bare.cfi, "precondition: the fallback locator carries no CFI")

        hydrated = self.manager._hydrate_cfi_locator(
            bare, "book.epub", "abs-1", "Title", "ABS", 0.5, self.formatter
        )

        self.assertIsNotNone(hydrated, "a Kobo-backed client cannot act on a percentage")
        self.assertEqual(hydrated.cfi, "epubcfi(/6/8!/4/2/6:0)")
        self.assertEqual(hydrated.href, "OEBPS/Text/part0007.xhtml")
        self.assertEqual(hydrated.chapter_progress, 0.42)

    def test_hydrated_locator_keeps_the_leader_percentage(self):
        """Hydration re-expresses the position; it must not move it."""
        _arm_hydration(self.manager)
        hydrated = self.manager._hydrate_cfi_locator(
            LocatorResult(percentage=0.5), "book.epub", "abs-1", "Title", "ABS",
            0.5, self.formatter,
        )
        self.assertAlmostEqual(hydrated.percentage, 0.5)

    def test_cfi_that_does_not_round_trip_is_rejected(self):
        """A locator that resolves somewhere else is worse than no locator."""
        _arm_hydration(self.manager, roundtrip=200)  # target was 5000 of 10000
        self.assertIsNone(
            self.manager._hydrate_cfi_locator(
                LocatorResult(percentage=0.5), "book.epub", "abs-1", "Title", "ABS",
                0.5, self.formatter,
            )
        )

    def test_collapse_to_start_of_book_is_blocked(self):
        """The collapse guard still applies to a hydrated locator.

        A match_index that fell through to char 0 while the leader sits at 80% is the
        shape this guards: the offset round-trips perfectly, so only the collapse
        check catches that the position silently rewound to the start of the book.
        """
        self.manager._get_cached_ebook_text = MagicMock(return_value=("x" * 10000, 10000))
        self.manager.ebook_parser.get_locator_from_char_offset = MagicMock(
            return_value=LocatorResult(percentage=0.0, cfi="epubcfi(/6/2!/4/2:0)")
        )
        self.manager.ebook_parser.resolve_cfi_to_index = MagicMock(return_value=0)

        collapsed = LocatorResult(percentage=0.8, match_index=0)
        self.assertIsNone(
            self.manager._hydrate_cfi_locator(
                collapsed, "book.epub", "abs-1", "Title", "ABS", 0.8, self.formatter,
            )
        )

    def test_genuine_reset_to_start_is_not_treated_as_a_collapse(self):
        """A leader that really is at 0% must still hydrate."""
        self.manager._get_cached_ebook_text = MagicMock(return_value=("x" * 10000, 10000))
        self.manager.ebook_parser.get_locator_from_char_offset = MagicMock(
            return_value=LocatorResult(percentage=0.0, cfi="epubcfi(/6/2!/4/2:0)")
        )
        self.manager.ebook_parser.resolve_cfi_to_index = MagicMock(return_value=0)

        hydrated = self.manager._hydrate_cfi_locator(
            LocatorResult(percentage=0.0), "book.epub", "abs-1", "Title", "ABS",
            0.0, self.formatter,
        )
        self.assertIsNotNone(hydrated)
        self.assertEqual(hydrated.cfi, "epubcfi(/6/2!/4/2:0)")

    def test_storyteller_slim_epub_is_skipped(self):
        _arm_hydration(self.manager)
        self.assertIsNone(
            self.manager._hydrate_cfi_locator(
                LocatorResult(percentage=0.5), "storyteller_123.epub", "abs-1",
                "Title", "ABS", 0.5, self.formatter,
            )
        )

    def test_missing_epub_is_skipped(self):
        _arm_hydration(self.manager)
        self.assertIsNone(
            self.manager._hydrate_cfi_locator(
                LocatorResult(percentage=0.5), None, "abs-1", "Title", "ABS",
                0.5, self.formatter,
            )
        )


class TestKoboBackedClientsAreRoutedTheHydratedLocator(unittest.TestCase):
    """The routing contract that keeps the Kobo converters fed.

    BookOrbit and Grimmory each build the KoboSpan themselves, but only from the CFI
    we send them, and CWA has no converter at all. Dropping any of these from the set
    silently reintroduces #364 for that service.
    """

    def test_every_kobo_backed_ebook_client_is_included(self):
        for client in ("BookOrbit", "BookLore", "CWA"):
            with self.subTest(client=client):
                self.assertIn(client, _CFI_DEPENDENT_CLIENTS)

    def test_abs_ebook_is_still_included(self):
        """ABSEbook rejects a cfi-less locator outright; it was the original case."""
        self.assertIn("ABSEbook", _CFI_DEPENDENT_CLIENTS)

    def test_kosync_is_not_included(self):
        """KoSync navigates by xpath and keeps its existing percentage behavior."""
        self.assertNotIn("KoSync", _CFI_DEPENDENT_CLIENTS)


class TestCWASpanSyncSetting(unittest.TestCase):
    """Fix 3 — CWA_KOBO_SPAN_SYNC gates writing a resolved span."""

    def setUp(self):
        from src.sync_clients.cwa_sync_client import CWASyncClient

        self.previous = os.environ.get("CWA_KOBO_SPAN_SYNC")
        self.sync_api = MagicMock()
        self.client = CWASyncClient(self.sync_api, MagicMock(), MagicMock())
        self.book = MagicMock()
        self.book.abs_title = "Test Book"
        self.book.ebook_source_id = "1519"
        self.locator = LocatorResult(
            percentage=0.36, cfi="epubcfi(/6/8!/4/2:0)",
            href="OEBPS/Text/part0024.xhtml", chapter_progress=0.5,
        )

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("CWA_KOBO_SPAN_SYNC", None)
        else:
            os.environ["CWA_KOBO_SPAN_SYNC"] = self.previous

    def _arm_span_map(self):
        span_map = MagicMock()
        span_map.chapters = [MagicMock()]
        self.client._get_span_map = MagicMock(return_value=span_map)
        import src.utils.kepub_locator as kl

        self._real_resolve = kl.resolve_span
        kl.resolve_span = MagicMock(
            return_value=("OEBPS/Text/part0024.xhtml", "kobo.114.4")
        )
        self.addCleanup(setattr, kl, "resolve_span", self._real_resolve)

    def test_disabled_never_writes_a_span(self):
        os.environ["CWA_KOBO_SPAN_SYNC"] = "false"
        self._arm_span_map()
        self.assertIsNone(self.client._resolve_kobo_location(self.book, "test-uuid", self.locator))

    def test_enabled_with_true_writes_the_span(self):
        os.environ["CWA_KOBO_SPAN_SYNC"] = "true"
        self._arm_span_map()
        self.assertEqual(
            self.client._resolve_kobo_location(self.book, "test-uuid", self.locator),
            {
                "Source": "OEBPS/Text/part0024.xhtml",
                "Type": "KoboSpan",
                "Value": "kobo.114.4",
            },
        )

    def test_enabled_with_on_writes_the_span(self):
        """Settings checkboxes POST 'on', not 'true' (CLAUDE.md failure mode #1)."""
        os.environ["CWA_KOBO_SPAN_SYNC"] = "on"
        self._arm_span_map()
        self.assertIsNotNone(
            self.client._resolve_kobo_location(self.book, "test-uuid", self.locator)
        )

    def test_unresolvable_span_leaves_the_device_bookmark_alone(self):
        os.environ["CWA_KOBO_SPAN_SYNC"] = "true"
        self.client._get_span_map = MagicMock(return_value=None)
        self.assertIsNone(self.client._resolve_kobo_location(self.book, "test-uuid", self.locator))

    def test_update_progress_sends_the_resolved_span(self):
        os.environ["CWA_KOBO_SPAN_SYNC"] = "true"
        self.sync_api.resolve_book_uuid.return_value = "test-uuid"
        self.sync_api.update_reading_state.return_value = True
        self.client._resolve_kobo_location = MagicMock(
            return_value={
                "Source": "OEBPS/Text/part0024.xhtml",
                "Type": "KoboSpan",
                "Value": "kobo.114.4",
            }
        )

        self.client.update_progress(
            self.book, UpdateProgressRequest(locator_result=self.locator)
        )

        _args, kwargs = self.sync_api.update_reading_state.call_args
        self.assertEqual(kwargs["location"]["Value"], "kobo.114.4")

    def test_span_map_miss_is_cached_so_the_kepub_is_not_refetched(self):
        os.environ["CWA_KOBO_SPAN_SYNC"] = "true"
        self.sync_api.download_kepub.return_value = None

        self.assertIsNone(self.client._get_span_map("test-uuid"))
        self.assertIsNone(self.client._get_span_map("test-uuid"))

        self.sync_api.download_kepub.assert_called_once_with("test-uuid")


if __name__ == "__main__":
    unittest.main()
