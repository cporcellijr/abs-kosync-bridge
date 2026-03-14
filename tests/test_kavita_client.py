import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.api.kavita_client import KavitaClient, KavitaKoSyncClient


class TestKavitaClient(unittest.TestCase):
    def test_clean_title_strips_icons_and_continue_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "DATA_DIR": tmp,
                    "KAVITA_ENABLED": "true",
                    "KAVITA_SERVER": "http://kavita.test",
                    "KAVITA_API_KEY": "token",
                },
                clear=False,
            ):
                client = KavitaClient()
                cleaned = client._clean_title("◔ Continue Reading From:   The Book Title  ")
                self.assertEqual(cleaned, "The Book Title")

    def test_find_book_by_filename_reads_kavita_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "DATA_DIR": tmp,
                    "KAVITA_ENABLED": "true",
                    "KAVITA_SERVER": "http://kavita.test",
                    "KAVITA_API_KEY": "token",
                },
                clear=False,
            ):
                client = KavitaClient()
                client._book_cache_by_id = {
                    "123": {"id": "123", "title": "Sample", "download_url": "http://kavita.test/file.epub"}
                }
                found = client.find_book_by_filename("kavita_123.epub", allow_refresh=False)
                self.assertIsNotNone(found)
                self.assertEqual(found["id"], "123")

    def test_kavita_kosync_client_base_url(self):
        with patch.dict(
            os.environ,
            {
                "KAVITA_ENABLED": "true",
                "KAVITA_SERVER": "kavita.local:5000",
                "KAVITA_API_KEY": "abc123",
            },
            clear=False,
        ):
            client = KavitaKoSyncClient()
            self.assertTrue(client.is_configured())
            self.assertEqual(client.base_url, "http://kavita.local:5000/api/koreader/abc123")

    def test_kavita_kosync_client_user_uses_kavita_setting(self):
        with patch.dict(
            os.environ,
            {
                "KOSYNC_USER": "standalone-user",
                "KAVITA_KOSYNC_USER": "bridge-user",
            },
            clear=False,
        ):
            client = KavitaKoSyncClient()
            self.assertEqual(client.user, "bridge-user")

    def test_add_to_collection_creates_collection_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "DATA_DIR": tmp,
                    "KAVITA_ENABLED": "true",
                    "KAVITA_SERVER": "http://kavita.test",
                    "KAVITA_API_KEY": "token",
                },
                clear=False,
            ):
                client = KavitaClient()
                client._auth_jwt = Mock(return_value="jwt-token")
                client.session.get = Mock(return_value=Mock(status_code=200, text="[]", json=Mock(return_value=[])))
                client.session.post = Mock(
                    side_effect=[
                        Mock(status_code=201, text='{"id": 55}', json=Mock(return_value={"id": 55})),
                        Mock(status_code=200, text="{}", json=Mock(return_value={})),
                    ]
                )

                result = client.add_to_collection("99")

                self.assertTrue(result)
                client.session.post.assert_any_call(
                    "http://kavita.test/api/Collection",
                    headers={"Authorization": "Bearer jwt-token"},
                    json={"title": "Bridge", "promoted": False},
                    timeout=10,
                )
                client.session.post.assert_any_call(
                    "http://kavita.test/api/Collection/update-series",
                    headers={"Authorization": "Bearer jwt-token"},
                    json={"id": 55, "seriesIds": [99]},
                    timeout=10,
                )


if __name__ == "__main__":
    unittest.main()
