import unittest
from unittest.mock import MagicMock
from types import SimpleNamespace

from src.db.models import Book
from src.sync_clients.kosync_sync_client import KoSyncSyncClient


class TestKoSyncSyncClientRouting(unittest.TestCase):
    def setUp(self):
        self.kosync_api = MagicMock()
        self.ebook_parser = MagicMock()

    def test_blocked_kavita_source(self):
        client = KoSyncSyncClient(
            self.kosync_api,
            self.ebook_parser,
            blocked_ebook_source="Kavita",
        )
        book = Book(
            abs_id="abs-1",
            ebook_filename="kavita_123.epub",
            ebook_source="Kavita",
        )
        self.assertFalse(client.supports_book(book))

    def test_blocked_kavita_source_from_filename_prefix(self):
        client = KoSyncSyncClient(
            self.kosync_api,
            self.ebook_parser,
            blocked_ebook_source="Kavita",
        )
        book = Book(abs_id="abs-1", ebook_filename="kavita_123.epub")
        self.assertFalse(client.supports_book(book))

    def test_allowed_kavita_source_only(self):
        client = KoSyncSyncClient(
            self.kosync_api,
            self.ebook_parser,
            allowed_ebook_source="Kavita",
        )
        kavita_book = Book(
            abs_id="abs-1",
            ebook_filename="kavita_123.epub",
            ebook_source="Kavita",
        )
        other_book = Book(
            abs_id="abs-2",
            ebook_filename="book.epub",
            ebook_source="Booklore",
        )
        self.assertTrue(client.supports_book(kavita_book))
        self.assertFalse(client.supports_book(other_book))

    def test_display_name_override_used_in_service_state(self):
        self.kosync_api.get_progress.return_value = (0.361111, "/body/DocFragment[14].0")
        self.kosync_api.is_configured.return_value = True

        client = KoSyncSyncClient(
            self.kosync_api,
            self.ebook_parser,
            display_name="KavitaKoSync",
        )
        book = Book(abs_id="abs-3", ebook_filename="kavita_187.epub", ebook_source="Kavita")

        state = client.get_service_state(book, None)

        self.assertEqual(state.display[0], "KavitaKoSync")

    def test_kavita_service_state_uses_locator_derived_percentage(self):
        self.kosync_api.get_progress.return_value = (0.14666666, "/body/DocFragment[12]/body/div/p[63].0")
        self.kosync_api.is_configured.return_value = True
        self.ebook_parser.resolve_xpath_to_index.return_value = 475
        self.ebook_parser.resolve_book_path.return_value = "kavita_474.epub"
        self.ebook_parser.extract_text_and_map.return_value = ("x" * 10000, [])

        client = KoSyncSyncClient(
            self.kosync_api,
            self.ebook_parser,
            display_name="KavitaKoSync",
        )
        book = Book(abs_id="abs-3", ebook_filename="kavita_474.epub", ebook_source="Kavita")

        state = client.get_service_state(book, None)

        self.assertAlmostEqual(state.current["pct"], 0.0475, places=4)
        self.assertEqual(state.current["_locator_pct"], state.current["pct"])
        self.assertEqual(state.current["_remote_pct"], 0.14666666)

    def test_kavita_service_state_uses_previous_xpath_for_delta(self):
        self.kosync_api.get_progress.return_value = (0.14666666, "/body/DocFragment[12]/body/div/p[63].0")
        self.kosync_api.is_configured.return_value = True
        self.ebook_parser.resolve_xpath_to_index.side_effect = [475, 452]
        self.ebook_parser.resolve_book_path.return_value = "kavita_474.epub"
        self.ebook_parser.extract_text_and_map.return_value = ("x" * 10000, [])

        client = KoSyncSyncClient(
            self.kosync_api,
            self.ebook_parser,
            display_name="KavitaKoSync",
        )
        book = Book(abs_id="abs-3", ebook_filename="kavita_474.epub", ebook_source="Kavita")
        prev_state = SimpleNamespace(percentage=0.14666666, xpath="/body/DocFragment[12]/body/div/p[53].0")

        state = client.get_service_state(book, prev_state)

        self.assertAlmostEqual(state.previous_pct, 0.0452, places=4)
        self.assertAlmostEqual(state.delta, 0.0023, places=4)

    def test_kavita_service_state_reuses_current_pct_for_equivalent_previous_xpath(self):
        self.kosync_api.get_progress.return_value = (0.14666666, "/body/DocFragment[12]/body/div/p[63].0")
        self.kosync_api.is_configured.return_value = True
        self.ebook_parser.resolve_xpath_to_index.return_value = 475
        self.ebook_parser.resolve_book_path.return_value = "kavita_474.epub"
        self.ebook_parser.extract_text_and_map.return_value = ("x" * 10000, [])

        client = KoSyncSyncClient(
            self.kosync_api,
            self.ebook_parser,
            display_name="KavitaKoSync",
        )
        book = Book(abs_id="abs-3", ebook_filename="kavita_474.epub", ebook_source="Kavita")
        prev_state = SimpleNamespace(percentage=0.16, xpath="/body/DocFragment[12]/body/div/p[63]/text().0")

        state = client.get_service_state(book, prev_state)

        self.assertAlmostEqual(state.previous_pct, state.current["pct"], places=6)
        self.assertAlmostEqual(state.delta, 0.0, places=6)
        self.ebook_parser.resolve_xpath_to_index.assert_called_once()


if __name__ == "__main__":
    unittest.main()
