import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))


class MockContainer:
    """Mock container implementing the web dependency contract."""

    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_booklore_client = Mock()
        self.mock_storyteller_client = Mock()
        self.mock_database_service = Mock()
        self.mock_ebook_parser = Mock()
        self.mock_forge_service = Mock()

        # Default DB behavior
        self.mock_database_service.get_all_settings.return_value = {}
        self.mock_database_service.get_all_books.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []
        self.mock_database_service.get_ignored_suggestion_source_ids.return_value = []
        self.mock_database_service.get_kosync_doc_by_filename.return_value = None
        self.mock_database_service.ignore_suggestion.return_value = True
        self.mock_database_service.get_book.return_value = None
        self.mock_database_service.get_book_by_kosync_id.return_value = None

        # Default manager behavior
        self.mock_sync_manager.abs_client = self.mock_abs_client
        self.mock_sync_manager.get_abs_title.return_value = "Regression Book"
        self.mock_sync_manager.get_duration.return_value = 3600

        # Default ABS behavior
        self.mock_abs_client.base_url = "http://abs.test"
        self.mock_abs_client.token = "token"
        self.mock_abs_client.get_all_audiobooks.return_value = [
            {
                "id": "ab-1",
                "media": {
                    "metadata": {"title": "Regression Book", "authorName": "Test Author"},
                    "duration": 3600,
                },
            }
        ]
        self.mock_abs_client.get_item_details.return_value = {
            "media": {
                "chapters": [{"start": 0.0, "end": 10.0}],
                "metadata": {"title": "Regression Book", "authorName": "Test Author"},
            }
        }

        # Default booklore behavior
        self.mock_booklore_client.is_configured.return_value = True
        self.mock_booklore_client.find_book_by_filename.return_value = {"id": "bl-1"}

        # Default storyteller behavior
        self.mock_storyteller_client.is_configured.return_value = False

        # Default sync clients map
        self._sync_clients = {
            "Hardcover": Mock(is_configured=Mock(return_value=False))
        }

    def sync_manager(self):
        return self.mock_sync_manager

    def abs_client(self):
        return self.mock_abs_client

    def booklore_client(self):
        return self.mock_booklore_client

    def storyteller_client(self):
        return self.mock_storyteller_client

    def ebook_parser(self):
        return self.mock_ebook_parser

    def forge_service(self):
        return self.mock_forge_service

    def database_service(self):
        return self.mock_database_service

    def sync_clients(self):
        return self._sync_clients

    def data_dir(self):
        return Path(tempfile.gettempdir()) / "test_data_match_paths"

    def books_dir(self):
        return Path(tempfile.gettempdir()) / "test_books_match_paths"

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / "test_epub_cache_match_paths"


class TestMatchPathsRegression(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["DATA_DIR"] = self.temp_dir
        os.environ["BOOKS_DIR"] = self.temp_dir

        self.mock_container = MockContainer()

        def _mock_initialize_database(_data_dir):
            return self.mock_container.mock_database_service

        import src.db.migration_utils

        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = _mock_initialize_database

        from src.web_server import create_app
        import src.web_server as web_server

        # Ensure isolated in-memory scan state per test run
        with web_server.SUGGESTIONS_SCAN_JOBS_LOCK:
            web_server.SUGGESTIONS_SCAN_JOBS.clear()
        with web_server.SUGGESTIONS_STATE_LOCK:
            web_server.SUGGESTIONS_STATE_STORE.clear()

        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        import shutil
        import src.db.migration_utils

        src.db.migration_utils.initialize_database = self.original_init_db
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _prepare_storyteller_assets(self, title: str, chapter_count: int = 2):
        assets_root = Path(self.temp_dir) / "storyteller_assets"
        transcriptions_dir = assets_root / "assets" / title / "transcriptions"
        transcriptions_dir.mkdir(parents=True, exist_ok=True)
        for idx in range(chapter_count):
            chapter_name = f"{idx + 1:05d}-00001.json"
            payload = {"transcript": f"chapter {idx + 1}", "wordTimeline": []}
            (transcriptions_dir / chapter_name).write_text(json.dumps(payload), encoding="utf-8")
        os.environ["STORYTELLER_ASSETS_DIR"] = str(assets_root)
        self.addCleanup(lambda: os.environ.pop("STORYTELLER_ASSETS_DIR", None))

    def _set_abs_chapters(self, chapter_count: int = 2):
        chapters = [{"start": idx * 10.0, "end": (idx + 1) * 10.0} for idx in range(chapter_count)]
        self.mock_container.mock_abs_client.get_item_details.return_value = {
            "media": {
                "chapters": chapters,
                "metadata": {"title": "Regression Book", "authorName": "Test Author"},
            }
        }

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-match-1")
    def test_match_route_creates_mapping(self, _mock_kosync):
        response = self.client.post(
            "/match",
            data={
                "audiobook_id": "ab-1",
                "ebook_filename": "book.epub",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.abs_id, "ab-1")
        self.assertEqual(saved_book.ebook_filename, "book.epub")
        self.assertEqual(saved_book.kosync_doc_id, "hash-match-1")
        self.assertEqual(saved_book.status, "pending")

        self.mock_container.mock_database_service.dismiss_suggestion.assert_any_call("ab-1")
        self.mock_container.mock_database_service.dismiss_suggestion.assert_any_call("hash-match-1")
        self.mock_container.mock_abs_client.add_to_collection.assert_called_once_with("ab-1", "Synced with KOReader")
        self.mock_container.mock_booklore_client.add_to_shelf.assert_called_once_with("book.epub", "Kobo")

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-match-story-real")
    def test_match_storyteller_uuid_real_ingest_persists_manifest(self, _mock_kosync):
        self._prepare_storyteller_assets("Regression Book", chapter_count=2)
        self._set_abs_chapters(chapter_count=2)
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        response = self.client.post(
            "/match",
            data={
                "audiobook_id": "ab-1",
                "ebook_filename": "book.epub",
                "storyteller_uuid": "story-uuid-match-real",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-match-real")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNotNone(saved_book.transcript_file)
        self.assertTrue(Path(saved_book.transcript_file).exists())

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-forge-1")
    def test_match_forge_action_only_stages(self, _mock_kosync):
        response = self.client.post(
            "/match",
            data={
                "action": "forge_match",
                "audiobook_id": "ab-1",
                "ebook_filename": "source.epub",
                "source_type": "Booklore",
                "source_id": "42",
                "source_path": "",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        staged_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(staged_book.abs_id, "ab-1")
        self.assertEqual(staged_book.ebook_filename, "source.epub")
        self.assertEqual(staged_book.kosync_doc_id, "hash-forge-1")
        self.assertEqual(staged_book.status, "forging")

        self.mock_container.mock_forge_service.start_auto_forge_match.assert_called_once()
        kwargs = self.mock_container.mock_forge_service.start_auto_forge_match.call_args.kwargs
        self.assertEqual(kwargs["abs_id"], "ab-1")
        self.assertEqual(kwargs["original_filename"], "source.epub")
        self.assertEqual(kwargs["original_hash"], "hash-forge-1")

        # Route should stage only; final linking side effects happen after forge completion.
        self.mock_container.mock_abs_client.add_to_collection.assert_not_called()
        self.mock_container.mock_booklore_client.add_to_shelf.assert_not_called()

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-1")
    def test_batch_match_add_and_process_queue(self, _mock_kosync):
        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch.epub",
                "ebook_display_name": "Batch Book",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        with self.client.session_transaction() as session_data:
            self.assertEqual(len(session_data.get("queue", [])), 1)
            self.assertEqual(session_data["queue"][0]["abs_id"], "ab-1")

        process_response = self.client.post(
            "/batch-match",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)
        self.assertTrue(process_response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.abs_id, "ab-1")
        self.assertEqual(processed_book.ebook_filename, "batch.epub")
        self.assertEqual(processed_book.kosync_doc_id, "hash-batch-1")

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("queue", []), [])

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-story-1")
    def test_batch_match_storyteller_uuid_preserves_storyteller_source(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-original.epub",
                "ebook_display_name": "Batch Story",
                "storyteller_uuid": "story-uuid-1",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/batch-match",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.storyteller_uuid, "story-uuid-1")
        self.assertEqual(processed_book.transcript_source, "storyteller")
        self.assertIsNone(processed_book.transcript_file)

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("queue", []), [])

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-batch-story-real")
    def test_batch_match_storyteller_uuid_real_ingest_persists_manifest(self, _mock_kosync):
        self._prepare_storyteller_assets("Regression Book", chapter_count=2)
        self._set_abs_chapters(chapter_count=2)
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/batch-match",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "batch-original.epub",
                "ebook_display_name": "Batch Story Real",
                "storyteller_uuid": "story-uuid-batch-real",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post("/batch-match", data={"action": "process_queue"})
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-batch-real")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNotNone(saved_book.transcript_file)
        self.assertTrue(Path(saved_book.transcript_file).exists())

    def test_batch_match_remove_from_queue(self):
        with self.client.session_transaction() as session_data:
            session_data["queue"] = [
                {"abs_id": "ab-1"},
                {"abs_id": "ab-2"},
            ]

        response = self.client.post(
            "/batch-match",
            data={"action": "remove_from_queue", "abs_id": "ab-1"},
        )
        self.assertEqual(response.status_code, 302)

        with self.client.session_transaction() as session_data:
            queue = session_data.get("queue", [])
            self.assertEqual(len(queue), 1)
            self.assertEqual(queue[0]["abs_id"], "ab-2")

    @patch("src.web_server._start_suggestions_scan_job", return_value="job-1")
    def test_suggestions_scan_ajax_and_status(self, _mock_start_job):
        scan_response = self.client.post(
            "/suggestions",
            data={"action": "scan"},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        self.assertEqual(scan_response.status_code, 200)
        payload = scan_response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["job_id"], "job-1")

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("suggestions_scan_job_id"), "job-1")

        with patch(
            "src.web_server._get_suggestions_scan_job",
            return_value={
                "status": "running",
                "error": None,
                "progress": {"phase": "scanning", "percent": 40},
            },
        ):
            status_response = self.client.get("/api/suggestions/scan-status")
            self.assertEqual(status_response.status_code, 200)
            status_payload = status_response.get_json()
            self.assertEqual(status_payload["status"], "running")
            self.assertEqual(status_payload["progress"]["percent"], 40)

        with patch(
            "src.web_server._get_suggestions_scan_job",
            return_value={
                "status": "done",
                "error": None,
                "progress": {"phase": "finalizing", "percent": 100},
                "results": {
                    "suggestions": [{"abs_id": "ab-1"}, {"abs_id": "ab-2"}],
                    "stats": {"scanned_new": 2, "reused_cached": 0},
                },
            },
        ):
            done_response = self.client.get("/api/suggestions/scan-status")
            self.assertEqual(done_response.status_code, 200)
            done_payload = done_response.get_json()
            self.assertEqual(done_payload["status"], "done")
            self.assertEqual(done_payload["count"], 2)
            self.assertEqual(done_payload["stats"]["scanned_new"], 2)

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-suggestions-1")
    def test_suggestions_queue_add_and_process(self, _mock_kosync):
        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggested.epub",
                "ebook_display_name": "Suggested Book",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        with self.client.session_transaction() as session_data:
            self.assertEqual(len(session_data.get("queue", [])), 1)
            self.assertEqual(session_data["queue"][0]["abs_id"], "ab-1")

        process_response = self.client.post(
            "/suggestions",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)
        self.assertTrue(process_response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("queue", []), [])

    @patch("src.web_server.ingest_storyteller_transcripts", return_value=None)
    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-suggestions-story-1")
    def test_suggestions_queue_storyteller_uuid_preserves_storyteller_source(self, _mock_kosync, _mock_ingest):
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggested-original.epub",
                "ebook_display_name": "Suggested Story",
                "storyteller_uuid": "story-uuid-2",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post(
            "/suggestions",
            data={"action": "process_queue"},
        )
        self.assertEqual(process_response.status_code, 302)
        self.assertTrue(process_response.location.endswith("/"))

        self.mock_container.mock_database_service.save_book.assert_called_once()
        processed_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(processed_book.storyteller_uuid, "story-uuid-2")
        self.assertEqual(processed_book.transcript_source, "storyteller")
        self.assertIsNone(processed_book.transcript_file)

        with self.client.session_transaction() as session_data:
            self.assertEqual(session_data.get("queue", []), [])

    @patch("src.web_server.get_kosync_id_for_ebook", return_value="hash-suggestions-story-real")
    def test_suggestions_queue_storyteller_uuid_real_ingest_persists_manifest(self, _mock_kosync):
        self._prepare_storyteller_assets("Regression Book", chapter_count=2)
        self._set_abs_chapters(chapter_count=2)
        self.mock_container.mock_storyteller_client.download_book.return_value = True

        add_response = self.client.post(
            "/suggestions",
            data={
                "action": "add_to_queue",
                "audiobook_id": "ab-1",
                "ebook_filename": "suggested-original.epub",
                "ebook_display_name": "Suggested Story Real",
                "storyteller_uuid": "story-uuid-suggestions-real",
            },
        )
        self.assertEqual(add_response.status_code, 302)

        process_response = self.client.post("/suggestions", data={"action": "process_queue"})
        self.assertEqual(process_response.status_code, 302)

        self.mock_container.mock_database_service.save_book.assert_called_once()
        saved_book = self.mock_container.mock_database_service.save_book.call_args[0][0]
        self.assertEqual(saved_book.storyteller_uuid, "story-uuid-suggestions-real")
        self.assertEqual(saved_book.transcript_source, "storyteller")
        self.assertIsNotNone(saved_book.transcript_file)
        self.assertTrue(Path(saved_book.transcript_file).exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
