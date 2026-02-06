import unittest
import os
from unittest.mock import MagicMock, patch
from src.api.cwa_client import CWAClient

class TestCWAClient(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict('os.environ', {
            'CWA_ENABLED': 'true',
            'CWA_SERVER': 'http://cwa:8083',
            'CWA_USERNAME': 'user',
            'CWA_PASSWORD': 'pass'
        })
        self.env_patcher.start()
        self.client = CWAClient()

    def tearDown(self):
        self.env_patcher.stop()

    @patch('requests.Session.get')
    def test_search_ebooks_parsing(self, mock_get):
        # Mock XML response (Atom)
        mock_response_content = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:opds="http://opds-spec.org/2010/catalog">
            <entry>
                <title>Test Book</title>
                <author>
                    <name>Test Author</name>
                </author>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip" href="/download/123/epub" />
            </entry>
        </feed>
        """
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = mock_response_content
        
        results = self.client.search_ebooks("Test Book")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['title'], 'Test Book')
        self.assertEqual(results[0]['author'], 'Test Author')
        self.assertEqual(results[0]['download_url'], 'http://cwa:8083/download/123/epub')

    @patch('requests.Session.get')
    def test_download_ebook(self, mock_get):
        mock_get.return_value.__enter__.return_value.status_code = 200
        mock_get.return_value.__enter__.return_value.iter_content.return_value = [b"fake content" * 100]
        
        with patch('builtins.open', unittest.mock.mock_open()) as mock_file:
            with patch('os.path.getsize', return_value=2000): # Mock size > 1024
                 success = self.client.download_ebook('http://url', 'test.epub')
                 self.assertTrue(success)
                 mock_file.assert_called_with('test.epub', 'wb')
