import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.api.hardcover_client import HardcoverClient, HardcoverRateLimitError


def _response(status_code, payload=None, text="", headers=None):
    response = Mock()
    response.status_code = status_code
    response.text = text
    response.headers = headers or {}
    response.json.return_value = payload or {}
    return response


class TestHardcoverClient(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {"HARDCOVER_TOKEN": "test-token", "HARDCOVER_ENABLED": "true"},
            clear=False,
        )
        self.env_patcher.start()
        self.client = HardcoverClient()

    def tearDown(self):
        self.env_patcher.stop()

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_query_retries_read_queries_on_429(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(429, text='{"error":"Throttled"}', headers={"Retry-After": "3"}),
            _response(200, payload={"data": {"search": {"ids": [1]}}}),
        ]

        result = self.client.query("query { search(query: \"test\") { ids } }")

        self.assertEqual(result, {"search": {"ids": [1]}})
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once_with(3.0)

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_query_uses_backoff_when_retry_after_missing(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(429, text='{"error":"Throttled"}'),
            _response(429, text='{"error":"Throttled"}'),
            _response(200, payload={"data": {"me": [{"id": 7}]}}),
        ]

        result = self.client.query("query { me { id } }")

        self.assertEqual(result, {"me": [{"id": 7}]})
        self.assertEqual(mock_sleep.call_args_list[0][0][0], 1.0)
        self.assertEqual(mock_sleep.call_args_list[1][0][0], 2.0)

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_query_raises_after_max_429_attempts(self, mock_post, mock_sleep):
        mock_post.side_effect = [
            _response(429, text='{"error":"Throttled"}'),
            _response(429, text='{"error":"Throttled"}'),
            _response(429, text='{"error":"Throttled"}'),
        ]

        with self.assertRaises(HardcoverRateLimitError):
            self.client.query("query { me { id } }")

        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("src.api.hardcover_client.time.sleep")
    @patch("src.api.hardcover_client.requests.post")
    def test_mutations_are_not_retried_on_429(self, mock_post, mock_sleep):
        mock_post.return_value = _response(429, text='{"error":"Throttled"}')

        result = self.client.query("mutation { insert_user_book(object: {}) { error } }")

        self.assertIsNone(result)
        mock_post.assert_called_once()
        mock_sleep.assert_not_called()

    def test_is_configured_uses_per_user_enabled_flag(self):
        # A user with Hardcover enabled + token is configured on their own account,
        # without inheriting the admin's. The global gate must be ON for the
        # per-user flag to have anything to decide (see the next test).
        with patch.dict(os.environ, {"HARDCOVER_ENABLED": "true"}, clear=False):
            client = HardcoverClient(credentials={
                "HARDCOVER_TOKEN": "user-token",
                "HARDCOVER_ENABLED": "true",
                "__allow_global_fallback__": False,
            })
            self.assertTrue(client.is_configured())

    def test_global_off_overrides_a_user_who_enabled_it(self):
        """A service switched off install-wide stays off for everyone. This used to
        be the other way round — a per-user 'true' beat a global 'false' — which is
        how a Storyteller nobody had enabled globally kept getting polled."""
        with patch.dict(os.environ, {"HARDCOVER_ENABLED": "false"}, clear=False):
            client = HardcoverClient(credentials={
                "HARDCOVER_TOKEN": "user-token",
                "HARDCOVER_ENABLED": "true",
                "__allow_global_fallback__": False,
            })
            self.assertFalse(client.is_configured())

    def test_is_configured_honors_per_user_disabled_flag(self):
        with patch.dict(os.environ, {"HARDCOVER_ENABLED": "true"}, clear=False):
            client = HardcoverClient(credentials={
                "HARDCOVER_TOKEN": "user-token",
                "HARDCOVER_ENABLED": "false",
                "__allow_global_fallback__": False,
            })
            self.assertFalse(client.is_configured())

    def test_update_status_returns_newest_active_read(self):
        """The status mutation returns the read created by a status transition."""
        self.client.query = Mock(return_value={
            "insert_user_book": {
                "error": None,
                "user_book": {
                    "id": 11,
                    "status_id": 2,
                    "user_book_reads": [{"id": 22}],
                },
            }
        })

        result = self.client.update_status(33, 2, 44)

        self.assertEqual(result["user_book_reads"], [{"id": 22}])
        mutation = self.client.query.call_args.args[0]
        self.assertIn("user_book_reads(order_by: {id: desc}, limit: 1)", mutation)

    def test_update_progress_targets_active_read_from_status_mutation(self):
        """Progress must not fall back to the stale read after a status promotion."""
        active_read = {
            "id": 22,
            "started_at": None,
            "finished_at": None,
        }
        self.client.query = Mock(return_value={
            "update_user_book_read": {
                "error": None,
                "user_book_read": {"id": 22},
            }
        })

        updated = self.client.update_progress(
            11,
            50,
            edition_id=44,
            current_percentage=0.25,
            active_read=active_read,
        )

        self.assertTrue(updated)
        self.client.query.assert_called_once()
        variables = self.client.query.call_args.args[1]
        self.assertEqual(variables["id"], 22)
        self.assertEqual(variables["pages"], 50)

    def test_ensure_list_reuses_existing_list(self):
        self.client.get_user_id = Mock(return_value=7)
        self.client.query = Mock(return_value={
            "lists": [{"id": 11, "name": "Grimmory: Fantasy"}]
        })

        result = self.client.ensure_list("Grimmory: Fantasy")

        self.assertEqual(result, {"id": 11, "name": "Grimmory: Fantasy"})
        self.client.query.assert_called_once()

    def test_create_list_returns_created_list(self):
        self.client.query = Mock(return_value={
            "insert_list": {
                "id": 12,
                "errors": None,
                "list": {"id": 12, "name": "Grimmory: Horror"},
            }
        })

        result = self.client.create_list("Grimmory: Horror", description="Managed list")

        self.assertEqual(result, {"id": 12, "name": "Grimmory: Horror"})
        variables = self.client.query.call_args[0][1]
        self.assertEqual(variables["object"]["name"], "Grimmory: Horror")
        self.assertEqual(variables["object"]["description"], "Managed list")
        self.assertEqual(variables["object"]["privacy_setting_id"], 3)
        self.assertFalse(variables["object"]["ranked"])

    def test_ensure_book_on_list_skips_existing_membership(self):
        self.client.ensure_list = Mock(return_value={"id": 21, "name": "Grimmory: Sci-Fi"})
        self.client.get_list_book = Mock(return_value={"id": 31, "list_id": 21, "book_id": 41})
        self.client.add_book_to_list = Mock()

        self.assertTrue(self.client.ensure_book_on_list("Grimmory: Sci-Fi", 41, edition_id=51))

        self.client.get_list_book.assert_called_once_with(21, 41)
        self.client.add_book_to_list.assert_not_called()

    def test_ensure_book_on_list_adds_missing_membership(self):
        self.client.ensure_list = Mock(return_value={"id": 21, "name": "Grimmory: Sci-Fi"})
        self.client.get_list_book = Mock(return_value=None)
        self.client.add_book_to_list = Mock(return_value={"id": 31})

        self.assertTrue(self.client.ensure_book_on_list("Grimmory: Sci-Fi", 41, edition_id=51))

        self.client.add_book_to_list.assert_called_once_with(21, 41, edition_id=51)

    def test_get_user_lists_returns_list_rows(self):
        self.client.get_user_id = Mock(return_value=7)
        self.client.query = Mock(return_value={
            "lists": [
                {"id": 11, "name": "Owned"},
                {"id": 12, "name": "Sci-Fi"},
            ]
        })

        result = self.client.get_user_lists()

        self.assertEqual(result, [{"id": 11, "name": "Owned"}, {"id": 12, "name": "Sci-Fi"}])
        variables = self.client.query.call_args[0][1]
        self.assertEqual(variables["userId"], 7)

    def test_get_list_book_memberships_queries_selected_lists(self):
        self.client.query = Mock(return_value={
            "list_books": [
                {"id": 31, "list_id": 11, "book_id": 41},
                {"id": 32, "list_id": 12, "book_id": 42},
            ]
        })

        result = self.client.get_list_book_memberships([11, 12])

        self.assertEqual(
            result,
            [
                {"id": 31, "list_id": 11, "book_id": 41},
                {"id": 32, "list_id": 12, "book_id": 42},
            ],
        )
        variables = self.client.query.call_args[0][1]
        self.assertEqual(variables["listIds"], [11, 12])

    def test_get_list_book_memberships_skips_empty_list_ids(self):
        self.client.query = Mock()

        self.assertEqual(self.client.get_list_book_memberships([]), [])

        self.client.query.assert_not_called()


class TestHardcoverAuthorGate(unittest.TestCase):
    """search_by_title_author must not commit a same-title/wrong-author book."""

    def setUp(self):
        self.env_patcher = patch.dict(
            os.environ,
            {"HARDCOVER_TOKEN": "test-token", "HARDCOVER_ENABLED": "true"},
            clear=False,
        )
        self.env_patcher.start()
        self.addCleanup(self.env_patcher.stop)
        self.client = HardcoverClient()

    def _candidate(self, book_id, title, author):
        return {
            "id": book_id,
            "title": title,
            "slug": f"slug-{book_id}",
            "cached_contributors": [{"name": author}] if author else [],
        }

    def test_rejects_exact_title_with_wrong_author(self):
        # "Stuck On You" by Jasper Bark, but the only candidate is Portia MacIntosh's.
        self.client._search_candidate_books = Mock(return_value=[
            self._candidate(1, "Stuck On You", "Portia MacIntosh"),
        ])
        self.client.get_default_edition = Mock(return_value={"id": 9, "pages": 320})

        result = self.client.search_by_title_author("Stuck On You", "Jasper Bark")
        self.assertIsNone(result)

    def test_accepts_exact_title_with_right_author(self):
        self.client._search_candidate_books = Mock(return_value=[
            self._candidate(1, "Stuck On You", "Portia MacIntosh"),
            self._candidate(2, "Stuck On You", "Jasper Bark"),
        ])
        self.client.get_default_edition = Mock(return_value={"id": 9, "pages": 200})

        result = self.client.search_by_title_author("Stuck On You", "Jasper Bark")
        self.assertIsNotNone(result)
        self.assertEqual(result["book_id"], 2)

    def test_title_only_search_unaffected_by_gate(self):
        # No author supplied -> gate does not apply, top title match still returned.
        self.client._search_candidate_books = Mock(return_value=[
            self._candidate(1, "Stuck On You", "Portia MacIntosh"),
        ])
        self.client.get_default_edition = Mock(return_value={"id": 9, "pages": 320})

        result = self.client.search_by_title_author("Stuck On You", "")
        self.assertIsNotNone(result)
        self.assertEqual(result["book_id"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
