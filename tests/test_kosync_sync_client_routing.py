import unittest
from unittest.mock import MagicMock

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


if __name__ == "__main__":
    unittest.main()
