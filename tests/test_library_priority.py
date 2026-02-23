import unittest
from unittest.mock import MagicMock, patch
import os
from src.services.library_service import LibraryService

class TestLibraryPriority(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_booklore = MagicMock()
        self.mock_cwa = MagicMock()
        self.mock_abs = MagicMock()
        self.epub_cache = '/tmp/cache'
        
        self.service = LibraryService(
            self.mock_db,
            self.mock_booklore,
            self.mock_cwa,
            self.mock_abs,
            self.epub_cache
        )

    @patch('os.path.exists')
    @patch('os.path.getsize')
    def test_priority_1_abs_direct(self, mock_getsize, mock_exists):
        # Setup: ABS has file
        mock_exists.return_value = False # Does not exist in cache logic
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        self.mock_abs.get_ebook_files.return_value = [{'stream_url': 'url', 'ext': 'epub'}]
        # Mock download success
        self.mock_abs.download_file.return_value = True
        
        # Act
        result = self.service.acquire_ebook(item)
        
        # Assert
        # Paths on windows might be different separators, using os.path.join
        expected = os.path.join(self.epub_cache, 'item1_direct.epub')
        self.assertEqual(result, expected)
        self.mock_abs.get_ebook_files.assert_called()
        self.mock_cwa.search_ebooks.assert_not_called() # Should stop at P1

    @patch('os.path.exists')
    @patch('os.path.getsize')
    def test_priority_3_cwa(self, mock_getsize, mock_exists):
        mock_exists.return_value = False
        # Setup: ABS direct match fails
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        self.mock_abs.get_ebook_files.return_value = []
        
        # CWA configured and finds match
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks.return_value = [{'download_url': 'durl', 'ext': 'epub'}]
        self.mock_cwa.download_ebook.return_value = True
        
        # Act
        result = self.service.acquire_ebook(item)
        
        # Assert
        expected = os.path.join(self.epub_cache, 'item1_cwa.epub')
        self.assertEqual(result, expected)
        self.mock_cwa.search_ebooks.assert_called()
        self.mock_abs.search_ebooks.assert_not_called() # Should stop at P3

    @patch('os.path.exists')
    @patch('os.path.getsize')
    def test_priority_4_abs_search(self, mock_getsize, mock_exists):
        mock_exists.return_value = False
        # Setup: ABS direct fail, CWA fail
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        # Mock get_ebook_files for initial check (empty) THEN for search result (found)
        # Side effect iterates through calls
        self.mock_abs.get_ebook_files.side_effect = [[], [{'stream_url': 'surl', 'ext': 'epub'}]]
        
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks.return_value = []
        
        # ABS Search finds match
        self.mock_abs.search_ebooks.return_value = [{'id': 'other1', 'author': 'A'}]
        self.mock_abs.download_file.return_value = True
        
        result = self.service.acquire_ebook(item)
        
        expected = os.path.join(self.epub_cache, 'item1_abs_search.epub')
        self.assertEqual(result, expected)
        self.mock_abs.search_ebooks.assert_called()

    @patch('os.path.exists')
    def test_fallback(self, mock_exists):
        # All fail
        item = {'id': 'item1', 'media': {'metadata': {'title': 'T', 'authorName': 'A'}}}
        self.mock_abs.get_ebook_files.return_value = []
        self.mock_cwa.is_configured.return_value = True
        self.mock_cwa.search_ebooks.return_value = []
        self.mock_abs.search_ebooks.return_value = []
        
        result = self.service.acquire_ebook(item)
        
        self.assertIsNone(result)
