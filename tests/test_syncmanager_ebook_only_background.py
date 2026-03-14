from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.db.models import Book
from src.sync_manager import SyncManager


def test_ebook_only_background_skips_transcript_pipeline_and_activates(tmp_path):
    manager = SyncManager.__new__(SyncManager)
    manager.abs_client = MagicMock()
    manager.booklore_client = MagicMock()
    manager.hardcover_client = MagicMock()
    manager.transcriber = MagicMock()
    manager.ebook_parser = MagicMock()
    manager.database_service = MagicMock()
    manager.storyteller_client = MagicMock()
    manager.alignment_service = MagicMock()
    manager.library_service = None
    manager.migration_service = None
    manager.data_dir = tmp_path
    manager.books_dir = tmp_path
    manager.epub_cache_dir = tmp_path / "epub_cache"

    epub_path = tmp_path / "book.epub"
    epub_path.write_text("dummy", encoding="utf-8")

    manager._get_local_epub = MagicMock(return_value=epub_path)
    manager.ebook_parser.extract_text_and_map.return_value = ("hello world", [])
    manager.database_service.update_latest_job = MagicMock()

    job = SimpleNamespace(retry_count=2, last_error="prev", progress=0.4)
    manager.database_service.get_latest_job.return_value = job

    book = Book(
        abs_id="ebook-abc123",
        abs_title="Ebook Only",
        ebook_filename="book.epub",
        kosync_doc_id="hash-1",
        sync_mode="ebook_only",
        status="processing",
    )

    manager._run_background_job(book, 1, 1)

    manager.abs_client.get_item_details.assert_not_called()
    manager.transcriber.transcribe_from_smil.assert_not_called()
    manager.transcriber.process_audio.assert_not_called()
    manager.abs_client.get_audio_files.assert_not_called()
    manager.alignment_service.align_and_store.assert_not_called()

    assert book.status == "active"
    manager.database_service.save_book.assert_called()
    manager.database_service.save_job.assert_called()
    assert job.progress == 1.0
    assert job.last_error is None
    assert job.retry_count == 0


def test_abs_trilink_background_prefers_storyteller_artifact_over_reacquiring_source(tmp_path):
    manager = SyncManager.__new__(SyncManager)
    manager.abs_client = MagicMock()
    manager.booklore_client = MagicMock()
    manager.hardcover_client = MagicMock()
    manager.transcriber = MagicMock()
    manager.ebook_parser = MagicMock()
    manager.database_service = MagicMock()
    manager.storyteller_client = MagicMock()
    manager.alignment_service = MagicMock()
    manager.audio_source_adapters = {}
    manager.migration_service = None
    manager.data_dir = tmp_path
    manager.books_dir = tmp_path
    manager.epub_cache_dir = tmp_path / "epub_cache"
    manager.library_service = MagicMock()

    story_epub = tmp_path / "storyteller_uuid-1.epub"
    story_epub.write_text("story", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")

    manager.abs_client.get_item_details.return_value = {"media": {"chapters": []}}
    manager._get_local_epub = MagicMock(side_effect=lambda name: story_epub if name == "storyteller_uuid-1.epub" else None)
    manager._get_storyteller_manifest_path = MagicMock(return_value=manifest_path)
    manager.ebook_parser.extract_text_and_map.return_value = ("story text", [])
    manager.alignment_service.align_storyteller_and_store.return_value = True
    manager.database_service.update_latest_job = MagicMock()

    job = SimpleNamespace(retry_count=1, last_error="prev", progress=0.2)
    manager.database_service.get_latest_job.return_value = job

    book = Book(
        abs_id="abs-1",
        abs_title="Tri Link ABS",
        ebook_filename="storyteller_uuid-1.epub",
        original_ebook_filename="kavita_187.epub",
        kosync_doc_id="hash-1",
        storyteller_uuid="uuid-1",
        transcript_source="storyteller",
        status="processing",
    )

    manager._run_background_job(book, 1, 1)

    manager._get_local_epub.assert_any_call("storyteller_uuid-1.epub")
    manager.library_service.acquire_ebook.assert_not_called()
    manager.ebook_parser.extract_text_and_map.assert_any_call(story_epub)
    assert book.ebook_filename == "storyteller_uuid-1.epub"
    assert book.status == "active"

