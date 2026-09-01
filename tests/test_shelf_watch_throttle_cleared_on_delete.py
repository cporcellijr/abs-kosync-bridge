#!/usr/bin/env python3
"""Regression tests: deleting a mapping must drop its shelf-watch throttle row.

Reported 2026-08-31. Two BookOrbit books were renamed in the library, their
bridge mappings deleted so the corrected titles would be picked up, and the
books put back on the 'Up Next' shelf. Every scan from then on logged:

    Shelf-watch on 'Up Next': scanned=3 auto=0 suggested=0 ebook_only=0
    skipped_existing=1 skipped_throttled=2 errors=0

The throttle row is keyed by the library's own book id ('bookorbit:5921'),
which survives both the rename and the mapping delete, so nothing about
delete-and-re-add reached the throttle: the books stayed skipped for the whole
BOOKORBIT_SHELF_WATCH_RESCAN_HOURS window.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db.database_service import DatabaseService
from src.db.models import Book
from src.services.shelf_watch_service import (
    ShelfWatchService,
    clear_shelf_watch_throttle,
    shelf_watch_scan_key,
)

SHELF_ENV = {
    'BOOKORBIT_SHELF_WATCH_ENABLED': 'true',
    'BOOKORBIT_SHELF_WATCH_NAME': 'Up Next',
    'BOOKORBIT_SHELF_WATCH_RESCAN_HOURS': '24',
    'BOOKORBIT_SHELF_NAME': 'Kobo',
}

SHELF_BOOK = {'id': '5921', 'fileName': "Mom's Fertility Clinic (2024).epub",
              'title': "Mom's Fertility Clinic"}


class TestShelfWatchThrottleClearedOnDelete(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = DatabaseService(str(Path(self.temp_dir) / 'test.db'))

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ---- helpers ----------------------------------------------------

    def _service(self, source_name='BookOrbit', env_prefix='BOOKORBIT'):
        client = MagicMock()
        client.is_configured.return_value = True
        client.list_books_on_shelf.return_value = [dict(SHELF_BOOK)]
        svc = ShelfWatchService(
            booklore_client=client,
            database_service=self.db,
            book_mapping_service=MagicMock(),
            source_name=source_name,
            env_prefix=env_prefix,
        )
        suggestions = MagicMock()
        suggestions._build_audiobook_candidate_pool.return_value = [{'audio_source': 'ABS'}]
        suggestions._scan_single_ebook.return_value = {'matches': []}
        svc.set_suggestions_service_factory(lambda: suggestions)
        return svc

    def _seed_throttle(self, key='bookorbit:5921'):
        self.db.upsert_shelf_watch_scan(
            key, SHELF_BOOK['fileName'], top_score=None, status='ebook_only',
        )

    @staticmethod
    def _deleted_book(source='BookOrbit', source_id='5921'):
        return Book(
            abs_id='ebook-4823e449146c781f',
            abs_title="Mom's Fertility Clinic",
            ebook_filename=SHELF_BOOK['fileName'],
            ebook_source=source,
            ebook_source_id=source_id,
            sync_mode='ebook_only',
        )

    # ---- DatabaseService helper -------------------------------------

    def test_delete_shelf_watch_scan_removes_the_row(self):
        self._seed_throttle()
        self.assertIsNotNone(self.db.get_shelf_watch_scan('bookorbit:5921'))

        self.assertTrue(self.db.delete_shelf_watch_scan('bookorbit:5921'))
        self.assertIsNone(self.db.get_shelf_watch_scan('bookorbit:5921'))

    def test_delete_shelf_watch_scan_unknown_or_empty_id_is_a_noop(self):
        self._seed_throttle()

        self.assertFalse(self.db.delete_shelf_watch_scan('bookorbit:9999'))
        self.assertFalse(self.db.delete_shelf_watch_scan(''))
        self.assertIsNotNone(
            self.db.get_shelf_watch_scan('bookorbit:5921'),
            'clearing an unrelated id dropped another book\'s throttle row',
        )

    # ---- clear_shelf_watch_throttle ---------------------------------

    def test_clear_throttle_uses_the_namespaced_key_for_bookorbit(self):
        self._seed_throttle()

        self.assertTrue(clear_shelf_watch_throttle(self.db, self._deleted_book()))
        self.assertIsNone(self.db.get_shelf_watch_scan('bookorbit:5921'))

    def test_clear_throttle_uses_the_bare_key_for_booklore(self):
        """BookLore rows keep the bare id for back-compat (see _scan_key)."""
        self._seed_throttle(key='5921')

        self.assertTrue(clear_shelf_watch_throttle(
            self.db, self._deleted_book(source='BookLore')))
        self.assertIsNone(self.db.get_shelf_watch_scan('5921'))
        self.assertEqual(shelf_watch_scan_key('BookLore', '5921'), '5921')

    def test_clear_throttle_without_an_ebook_source_is_a_noop(self):
        self._seed_throttle()

        self.assertFalse(clear_shelf_watch_throttle(
            self.db, self._deleted_book(source=None)))
        self.assertFalse(clear_shelf_watch_throttle(
            self.db, self._deleted_book(source_id=None)))
        self.assertIsNotNone(self.db.get_shelf_watch_scan('bookorbit:5921'))

    def test_clear_throttle_swallows_database_errors(self):
        broken = MagicMock()
        broken.delete_shelf_watch_scan.side_effect = RuntimeError('db gone')

        self.assertFalse(clear_shelf_watch_throttle(broken, self._deleted_book()))

    # ---- the reporter's sequence ------------------------------------

    def test_rescan_happens_after_the_mapping_is_deleted(self):
        """Delete the mapping, re-add to the shelf: the book is processed again."""
        svc = self._service()
        self._seed_throttle()

        with patch.dict(os.environ, SHELF_ENV, clear=False):
            throttled = svc._process_watch_shelf()
            self.assertEqual(throttled['skipped_throttled'], 1, 'seed did not take')

            clear_shelf_watch_throttle(self.db, self._deleted_book())
            rescanned = svc._process_watch_shelf()

        self.assertEqual(rescanned['skipped_throttled'], 0,
                         'the re-added book is still throttled after its mapping was deleted')
        self.assertEqual(rescanned['ebook_only'], 1)

    def test_throttle_still_applies_without_a_delete(self):
        """The throttle must keep suppressing books nobody deleted."""
        svc = self._service()
        self._seed_throttle()

        with patch.dict(os.environ, SHELF_ENV, clear=False):
            first = svc._process_watch_shelf()
            second = svc._process_watch_shelf()

        self.assertEqual(first['skipped_throttled'], 1)
        self.assertEqual(second['skipped_throttled'], 1)


class TestMappingDeleteClearsThrottle(unittest.TestCase):
    """The delete route's cleanup must reach the throttle, not just the helper."""

    def test_cleanup_mapping_resources_clears_the_throttle(self):
        import src.web_server as web_server

        book = Book(
            abs_id='ebook-4823e449146c781f',
            abs_title="Mom's Fertility Clinic",
            ebook_filename=None,
            ebook_source='BookOrbit',
            ebook_source_id='5921',
            sync_mode='ebook_only',
        )
        fake_db = MagicMock()
        fake_db.get_all_books.return_value = []
        fake_db.delete_kosync_data_for_book.return_value = (0, 0)

        data_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(data_dir), True)

        with (
            patch.object(web_server, 'uc', return_value=MagicMock()),
            patch.object(web_server, 'DATA_DIR', data_dir, create=True),
            patch.object(web_server, 'database_service', fake_db),
            patch.object(web_server, 'container', MagicMock()),
            patch('src.services.shelf_watch_service.clear_shelf_watch_throttle') as clear,
        ):
            web_server.cleanup_mapping_resources(book)

        clear.assert_called_once()
        self.assertIs(clear.call_args[0][1], book)


if __name__ == '__main__':
    unittest.main(verbosity=2)
