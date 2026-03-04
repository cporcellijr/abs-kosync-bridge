import json
from pathlib import Path
from unittest.mock import MagicMock

from src.db.models import Book
from src.sync_manager import SyncManager


def _write_manifest(base_dir: Path) -> Path:
    story_dir = base_dir / "transcripts" / "storyteller" / "abs-direct"
    story_dir.mkdir(parents=True, exist_ok=True)

    chapter_name = "00000-00001.json"
    chapter_payload = {
        "transcript": "hello world",
        "wordTimeline": [
            {
                "type": "word",
                "text": "hello",
                "startTime": 0.5,
                "endTime": 0.9,
                "startOffsetUtf16": 0,
                "endOffsetUtf16": 5,
                "timeline": [],
            },
            {
                "type": "word",
                "text": "world",
                "startTime": 1.0,
                "endTime": 1.4,
                "startOffsetUtf16": 6,
                "endOffsetUtf16": 11,
                "timeline": [],
            },
        ],
    }
    (story_dir / chapter_name).write_text(json.dumps(chapter_payload), encoding="utf-8")

    manifest_payload = {
        "format": "storyteller_manifest",
        "version": 1,
        "duration": 2.0,
        "chapters": [
            {"index": 0, "file": chapter_name, "start": 0.0, "end": 2.0, "text_len": 11, "text_len_utf16": 11},
        ],
    }
    manifest_path = story_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    return manifest_path


def test_storyteller_direct_locator_path_bypasses_fuzzy_search(tmp_path):
    manifest_path = _write_manifest(tmp_path)
    mock_locator = MagicMock()

    ebook_parser = MagicMock()
    ebook_parser.get_locator_from_char_offset.return_value = mock_locator
    ebook_parser.find_text_location = MagicMock()

    manager = SyncManager(
        abs_client=MagicMock(),
        booklore_client=MagicMock(),
        hardcover_client=MagicMock(),
        transcriber=MagicMock(),
        ebook_parser=ebook_parser,
        database_service=MagicMock(),
        storyteller_client=MagicMock(),
        sync_clients={},
        alignment_service=MagicMock(),
        library_service=None,
        migration_service=None,
        epub_cache_dir=tmp_path / "epub_cache",
        data_dir=tmp_path,
        books_dir=tmp_path / "books",
    )

    book = Book(
        abs_id="abs-direct",
        abs_title="Direct Locator",
        ebook_filename="book.epub",
        transcript_file=str(manifest_path),
        transcript_source="storyteller",
        status="active",
    )

    locator, txt = manager._resolve_storyteller_locator_from_abs_timestamp(book, 1.05)
    assert locator is mock_locator
    assert isinstance(txt, str)
    ebook_parser.get_locator_from_char_offset.assert_called_once()
    ebook_parser.find_text_location.assert_not_called()
