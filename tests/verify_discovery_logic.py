
import unittest
from unittest.mock import MagicMock, patch, ANY
import sys
import os
import json
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.kosync_server import init_kosync_server, kosync_put_progress, kosync_bp
from flask import Flask

class TestDiscoveryLogic(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(kosync_bp)
        
        # Mocks
        self.mock_db = MagicMock()
        self.mock_container = MagicMock()
        self.mock_manager = MagicMock()
        self.mock_abs_client = MagicMock()
        
        # Setup container to return mock abs client
        self.mock_container.abs_client.return_value = self.mock_abs_client
        self.mock_container.abs_client().is_configured.return_value = True
        
        # Initialize the server with mocks
        init_kosync_server(self.mock_db, self.mock_container, self.mock_manager)
        
        # Environment variables
        os.environ['KOSYNC_USER'] = 'user'
        os.environ['KOSYNC_KEY'] = 'pass'
        os.environ['AUTO_CREATE_EBOOK_MAPPING'] = 'true'

    @patch('src.api.kosync_server._try_find_epub_by_hash')
    @patch('src.api.kosync_server.threading.Thread')
    def test_audiobook_match_creates_suggestion(self, mock_thread, mock_find_epub):
        """Test asking for suggestion when audiobook match exists"""
        # Setup: EPUB found
        mock_find_epub.return_value = "Harry Potter 1.epub"
        
        # Setup: Audiobook found in ABS
        self.mock_abs_client.get_all_audiobooks.return_value = [{
            'id': 'abs-audio-1',
            'media': {
                'metadata': {'title': 'Harry Potter and the Sorcerers Stone', 'authorName': 'J.K. Rowling'},
                'duration': 1000
            }
        }]
        
        # Setup executing thread immediately
        def run_sync(target, args, daemon):
            target(*args)
            return MagicMock()
        mock_thread.side_effect = run_sync
        
        # Setup: No existing doc, no existing link
        self.mock_db.get_kosync_document.return_value = None
        self.mock_db.get_book_by_kosync_id.return_value = None
        # No existing suggestion
        self.mock_db.get_pending_suggestion.return_value = None
        
        # Action
        with self.app.test_request_context(
            '/syncs/progress',
            method='PUT',
            json={
                'document': 'hash123',
                'percentage': 0.1,
                'device': 'kobo'
            },
            headers={'x-auth-user': 'user', 'x-auth-key': 'pass'}
        ):
            kosync_put_progress()
            
        # Verify
        # 1. Should save pending suggestion
        self.mock_db.save_pending_suggestion.assert_called_once()
        call_args = self.mock_db.save_pending_suggestion.call_args[0][0]
        self.assertEqual(call_args.source_id, 'hash123')
        self.assertIn('Harry Potter 1', call_args.title)
        
        # 2. Should NOT save book (auto-mapping)
        self.mock_db.save_book.assert_not_called()
        print("\n✅ Test Passed: Created suggestion for audiobook match")

    @patch('src.api.kosync_server._try_find_epub_by_hash')
    @patch('src.api.kosync_server.threading.Thread')
    def test_no_match_fallback_to_ebook_only(self, mock_thread, mock_find_epub):
        """Test fallback to ebook-only when no audiobook match"""
        # Setup: EPUB found
        mock_find_epub.return_value = "Unknown Book.epub"
        
        # Setup: NO Audiobook in ABS
        self.mock_abs_client.get_all_audiobooks.return_value = []
        
        # Setup executing thread immediately
        def run_sync(target, args, daemon):
            target(*args)
            return MagicMock()
        mock_thread.side_effect = run_sync
        
        # Setup DB mocks
        self.mock_db.get_kosync_document.return_value = None
        self.mock_db.get_book_by_kosync_id.return_value = None
        
        # Action
        with self.app.test_request_context(
            '/syncs/progress',
            method='PUT',
            json={
                'document': 'hash456',
                'percentage': 0.1,
                'device': 'kobo'
            },
            headers={'x-auth-user': 'user', 'x-auth-key': 'pass'}
        ):
            kosync_put_progress()
            
        # Verify
        # 1. Should NOT save suggestion
        self.mock_db.save_pending_suggestion.assert_not_called()
        
        # 2. Should save book (ebook-only)
        self.mock_db.save_book.assert_called_once()
        saved_book = self.mock_db.save_book.call_args[0][0]
        self.assertEqual(saved_book.sync_mode, 'ebook_only')
        self.assertEqual(saved_book.ebook_filename, 'Unknown Book.epub')
        
        print("\n✅ Test Passed: Created ebook-only mapping (fallback)")

if __name__ == '__main__':
    unittest.main()
