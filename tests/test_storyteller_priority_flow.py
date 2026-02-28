import json
from pathlib import Path
from unittest.mock import MagicMock

from src.db.models import Book
from src.sync_manager import SyncManager


def _write_storyteller_manifest(base_dir: Path, abs_id: str) -> Path:
    target_dir = base_dir / "transcripts" / "storyteller" / abs_id
    target_dir.mkdir(parents=True, exist_ok=True)

    chapter_name = "00000-00001.json"
    chapter_payload = {
        "transcript": "hello world",
        "wordTimeline": [
            {
                "type": "word",
                "text": "hello",
                "startTime": 0.5,
                "endTime": 1.0,
                "startOffsetUtf16": 0,
                "endOffsetUtf16": 5,
                "timeline": [],
            },
            {
                "type": "word",
                "text": "world",
                "startTime": 1.0,
                "endTime": 1.5,
                "startOffsetUtf16": 6,
                "endOffsetUtf16": 11,
                "timeline": [],
            },
        ],
    }
    (target_dir / chapter_name).write_text(json.dumps(chapter_payload), encoding="utf-8")

    manifest_payload = {
        "format": "storyteller_manifest",
        "version": 1,
        "duration": 12.0,
        "chapters": [
            {"index": 0, "file": chapter_name, "start": 0.0, "end": 12.0},
        ],
    }
    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    return manifest_path


def test_storyteller_branch_skips_smil_and_whisper(tmp_path):
    abs_id = "abs-story-1"
    manifest_path = _write_storyteller_manifest(tmp_path, abs_id)

    db = MagicMock()
    db.get_books_by_status.return_value = []
    db.update_latest_job.return_value = None
    db.get_latest_job.return_value = MagicMock(retry_count=0, progress=0.0)

    abs_client = MagicMock()
    abs_client.get_item_details.return_value = {
        "media": {"chapters": [{"start": 0.0, "end": 12.0}]}
    }

    transcriber = MagicMock()
    transcriber.transcribe_from_smil = MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "unused"}])
    transcriber.process_audio = MagicMock(return_value=[{"start": 0.0, "end": 1.0, "text": "unused"}])

    ebook_parser = MagicMock()
    ebook_parser.extract_text_and_map.return_value = ("ebook text", [])

    alignment_service = MagicMock()
    alignment_service.align_storyteller_and_store.return_value = True
    alignment_service.align_and_store.return_value = True

    manager = SyncManager(
        abs_client=abs_client,
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=transcriber,
        ebook_parser=ebook_parser,
        database_service=db,
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=alignment_service,
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )

    epub_path = tmp_path / "book.epub"
    epub_path.write_text("dummy", encoding="utf-8")
    manager._get_local_epub = MagicMock(return_value=epub_path)

    book = Book(
        abs_id=abs_id,
        abs_title="Storyteller Book",
        ebook_filename=epub_path.name,
        kosync_doc_id="hash-1",
        status="pending",
        duration=12.0,
        transcript_file=str(manifest_path),
        transcript_source="storyteller",
    )

    manager._run_background_job(book)

    alignment_service.align_storyteller_and_store.assert_called_once()
    transcriber.transcribe_from_smil.assert_not_called()
    transcriber.process_audio.assert_not_called()
    abs_client.get_audio_files.assert_not_called()
