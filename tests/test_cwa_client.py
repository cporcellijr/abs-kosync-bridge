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

    def test_get_book_by_id_rejects_missing_identifier(self):
        with patch.object(self.client.session, 'get') as mock_get:
            self.assertIsNone(self.client.get_book_by_id(None))
            self.assertIsNone(self.client.get_book_by_id('None'))

        mock_get.assert_not_called()

    # -- get_book_uuid (issue #427: series search must resolve the right book) --

    # A CWA series search: "The Butcher's Masquerade" is returned first, the
    # selected book "Dungeon Crawler Carl" is fourth. CWA uses urn:uuid atom ids
    # and /opds/download/<id>/<fmt>/ acquisition links.
    _SERIES_FEED = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
        <entry>
            <title>The Butcher's Masquerade</title>
            <id>urn:uuid:6eae08f0-1622-4287-a767-359f84f15834</id>
            <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                  href="/opds/download/509/epub/" />
        </entry>
        <entry>
            <title>Dungeon Crawler Carl</title>
            <id>urn:uuid:d02f40b4-873a-4d04-8c56-ffcf3033979d</id>
            <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                  href="/opds/download/505/epub/" />
        </entry>
    </feed>
    """

    def _mock_search(self, mock_get, feed):
        # Skip endpoint discovery so the mock only serves the search response.
        self.client.search_template = 'http://cwa:8083/opds/search/{searchTerms}'
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = feed

    @patch('requests.Session.get')
    def test_get_book_uuid_selects_matching_series_entry(self, mock_get):
        # Stored id is the title-derived slug; must resolve to Dungeon Crawler
        # Carl's UUID, not the first (The Butcher's Masquerade) entry.
        self._mock_search(mock_get, self._SERIES_FEED)
        uuid = self.client.get_book_uuid('Dungeon_Crawler_Carl')
        self.assertEqual(uuid, 'd02f40b4-873a-4d04-8c56-ffcf3033979d')

    @patch('requests.Session.get')
    def test_get_book_uuid_ambiguous_returns_none(self, mock_get):
        # A multi-result search with no entry matching the stored id must not
        # guess — it returns None so the sync is skipped rather than corrupting
        # another book's progress.
        self._mock_search(mock_get, self._SERIES_FEED)
        uuid = self.client.get_book_uuid('Some_Other_Book')
        self.assertIsNone(uuid)

    @patch('requests.Session.get')
    def test_get_book_uuid_single_result_is_unambiguous(self, mock_get):
        single_feed = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
            <entry>
                <title>Solo Book</title>
                <id>urn:uuid:11111111-2222-3333-4444-555555555555</id>
                <link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
                      href="/opds/download/700/epub/" />
            </entry>
        </feed>
        """
        self._mock_search(mock_get, single_feed)
        uuid = self.client.get_book_uuid('Solo_Book')
        self.assertEqual(uuid, '11111111-2222-3333-4444-555555555555')

    @patch('requests.Session.get')
    def test_get_book_uuid_matches_numeric_download_id(self, mock_get):
        # When the stored id is the numeric Calibre id, match it against the
        # download link even though the entry is not first in the feed.
        self._mock_search(mock_get, self._SERIES_FEED)
        uuid = self.client.get_book_uuid('505')
        self.assertEqual(uuid, 'd02f40b4-873a-4d04-8c56-ffcf3033979d')
