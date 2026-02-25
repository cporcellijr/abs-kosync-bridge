"""
Tests for ABSSocketListener debounce logic and KoSync PUT instant sync trigger.
"""

import threading
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock

from src.services.abs_socket_listener import ABSSocketListener


class TestABSSocketListenerDebounce(unittest.TestCase):
    """Test the debounce logic in ABSSocketListener."""

    def setUp(self):
        """Create listener with mocked dependencies."""
        self.mock_db = MagicMock()
        self.mock_sync = MagicMock()

        with patch("src.services.abs_socket_listener.socketio.Client"):
            self.listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378",
                abs_api_token="test-token",
                database_service=self.mock_db,
                sync_manager=self.mock_sync,
            )
        # Override debounce window to 1s for fast tests
        self.listener._debounce_window = 1

    def _make_active_book(self, abs_id: str, title: str = "Test Book"):
        book = MagicMock()
        book.abs_id = abs_id
        book.abs_title = title
        book.status = "active"
        return book

    def test_ignores_non_active_books(self):
        """Events for books not in DB or not active should be ignored."""
        # Book not in DB
        self.mock_db.get_book.return_value = None
        self.listener._handle_progress_event({"id": "prog-1", "data": {"libraryItemId": "unknown-id"}})

        self.assertEqual(len(self.listener._pending), 0)

        # Book exists but not active
        inactive = self._make_active_book("inactive-id")
        inactive.status = "pending"
        self.mock_db.get_book.return_value = inactive
        self.listener._handle_progress_event({"id": "prog-2", "data": {"libraryItemId": "inactive-id"}})

        self.assertEqual(len(self.listener._pending), 0)

    def test_records_active_book_event(self):
        """Events for active books should be recorded in pending dict."""
        book = self._make_active_book("book-1")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "prog-3", "data": {"libraryItemId": "book-1"}})

        self.assertIn("book-1", self.listener._pending)

    def test_debounce_does_not_fire_before_window(self):
        """Sync should NOT fire if debounce window hasn't elapsed."""
        book = self._make_active_book("book-2")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "prog-4", "data": {"libraryItemId": "book-2"}})
        self.listener._check_and_fire()

        self.mock_sync.sync_cycle.assert_not_called()

    def test_debounce_fires_after_window(self):
        """Sync SHOULD fire after debounce window elapses."""
        book = self._make_active_book("book-3", "Debounce Test")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"id": "prog-5", "data": {"libraryItemId": "book-3"}})

        # Simulate time passing
        self.listener._pending["book-3"] = time.time() - 2  # 2s ago, window is 1s
        self.listener._check_and_fire()

        # Give the daemon thread a moment
        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once_with(target_abs_id="book-3")

    def test_no_double_fire(self):
        """Same event should not trigger sync twice."""
        book = self._make_active_book("book-4")
        self.mock_db.get_book.return_value = book

        self.listener._pending["book-4"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)

        # First fire should have removed from pending
        self.assertEqual(len(self.listener._pending), 0)

        # Calling again should do nothing
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.mock_sync.sync_cycle.assert_called_once()

    def test_new_event_after_fire_retriggers(self):
        """A new event after sync fired should start a fresh debounce."""
        book = self._make_active_book("book-5")
        self.mock_db.get_book.return_value = book

        # First event + fire
        self.listener._pending["book-5"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.assertEqual(self.mock_sync.sync_cycle.call_count, 1)

        # New event
        self.listener._handle_progress_event({"id": "prog-6", "data": {"libraryItemId": "book-5"}})
        self.assertIn("book-5", self.listener._pending)

        # Fire again after window
        self.listener._pending["book-5"] = time.time() - 2
        self.listener._check_and_fire()
        time.sleep(0.1)
        self.assertEqual(self.mock_sync.sync_cycle.call_count, 2)

    def test_handles_nested_data_format(self):
        """Should handle the real ABS event format: {id, sessionId, data: {libraryItemId}}."""
        book = self._make_active_book("nested-id")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({
            "id": "34621755-32df-4876-b235-abc123",
            "sessionId": "session-1",
            "deviceDescription": "Windows 10 / Firefox",
            "data": {"libraryItemId": "nested-id", "progress": 0.42}
        })
        self.assertIn("nested-id", self.listener._pending)

    def test_handles_top_level_library_item_id(self):
        """Should handle older ABS format with top-level libraryItemId."""
        book = self._make_active_book("top-level-id")
        self.mock_db.get_book.return_value = book

        self.listener._handle_progress_event({"libraryItemId": "top-level-id"})
        self.assertIn("top-level-id", self.listener._pending)

    def test_handles_missing_library_item_id(self):
        """Should silently ignore events with no libraryItemId."""
        self.listener._handle_progress_event({"someOtherField": "value"})
        self.assertEqual(len(self.listener._pending), 0)
        self.mock_db.get_book.assert_not_called()

    def test_url_stripping(self):
        """Server URL should strip trailing /api for socket connection."""
        with patch("src.services.abs_socket_listener.socketio.Client"):
            listener = ABSSocketListener(
                abs_server_url="http://abs.local:13378/api",
                abs_api_token="tok",
                database_service=MagicMock(),
                sync_manager=MagicMock(),
            )
        self.assertEqual(listener._server_url, "http://abs.local:13378")


class TestKosyncPutInstantSync(unittest.TestCase):
    """Test that KoSync PUT triggers instant sync for active linked books."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault('DATA_DIR', '/tmp/test_kosync_instant')
        os.environ.setdefault('KOSYNC_USER', 'testuser')
        os.environ.setdefault('KOSYNC_KEY', 'testpass')

        import hashlib
        cls.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json'
        }

    def test_put_triggers_sync_for_active_linked_book(self):
        """PUT progress for a linked active book should fire sync_cycle."""
        from src.api.kosync_server import _manager, kosync_put_progress
        import src.api.kosync_server as ks

        # Save original
        original_manager = ks._manager

        mock_manager = MagicMock()
        ks._manager = mock_manager

        mock_db = MagicMock()
        original_db = ks._database_service
        ks._database_service = mock_db

        try:
            from src.db.models import KosyncDocument, Book

            # Setup: linked active book
            mock_book = MagicMock()
            mock_book.abs_id = "test-instant-sync"
            mock_book.abs_title = "Instant Sync Book"
            mock_book.status = "active"
            mock_book.kosync_doc_id = "x" * 32

            mock_doc = MagicMock(spec=KosyncDocument)
            mock_doc.linked_abs_id = "test-instant-sync"
            mock_doc.percentage = 0.3
            mock_doc.device_id = "D1"

            mock_db.get_kosync_document.return_value = mock_doc
            mock_db.get_book.return_value = mock_book
            mock_db.get_book_by_kosync_id.return_value = None

            from flask import Flask
            app = Flask(__name__)
            with app.test_request_context(
                '/syncs/progress',
                method='PUT',
                json={
                    'document': 'x' * 32,
                    'percentage': 0.55,
                    'progress': '/body/test',
                    'device': 'TestDevice',
                    'device_id': 'D1'
                },
                content_type='application/json'
            ):
                from flask import request
                kosync_put_progress.__wrapped__()

            # sync_cycle should have been called in a thread
            time.sleep(0.2)
            mock_manager.sync_cycle.assert_called_once_with(target_abs_id="test-instant-sync")

        finally:
            ks._manager = original_manager
            ks._database_service = original_db

    def test_put_does_not_trigger_sync_for_inactive_book(self):
        """PUT for a linked but inactive book should NOT fire sync_cycle."""
        import src.api.kosync_server as ks

        original_manager = ks._manager
        mock_manager = MagicMock()
        ks._manager = mock_manager

        mock_db = MagicMock()
        original_db = ks._database_service
        ks._database_service = mock_db

        try:
            mock_book = MagicMock()
            mock_book.abs_id = "test-inactive"
            mock_book.abs_title = "Inactive Book"
            mock_book.status = "pending"

            mock_doc = MagicMock()
            mock_doc.linked_abs_id = "test-inactive"
            mock_doc.percentage = 0.1
            mock_doc.device_id = "D1"

            mock_db.get_kosync_document.return_value = mock_doc
            mock_db.get_book.return_value = mock_book
            mock_db.get_book_by_kosync_id.return_value = None

            from flask import Flask
            app = Flask(__name__)
            with app.test_request_context(
                '/syncs/progress',
                method='PUT',
                json={
                    'document': 'y' * 32,
                    'percentage': 0.2,
                    'progress': '/body/test',
                    'device': 'TestDevice',
                    'device_id': 'D1'
                },
                content_type='application/json'
            ):
                kosync_put_progress = ks.kosync_put_progress
                kosync_put_progress.__wrapped__()

            time.sleep(0.1)
            mock_manager.sync_cycle.assert_not_called()

        finally:
            ks._manager = original_manager
            ks._database_service = original_db


if __name__ == "__main__":
    unittest.main()
