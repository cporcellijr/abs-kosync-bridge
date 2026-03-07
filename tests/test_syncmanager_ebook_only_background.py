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

