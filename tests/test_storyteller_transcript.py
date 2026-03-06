import json
from pathlib import Path
from unittest.mock import MagicMock

from src.utils.storyteller_transcript import StorytellerTranscript
from src.utils.transcriber import AudioTranscriber
from src.services.alignment_service import (
    _storyteller_filename_for_abs_chapter,
    _validate_storyteller_chapters,
    ingest_storyteller_transcripts,
)


def _chapter_payload(transcript_text, words):
    return {
        "transcript": transcript_text,
        "wordTimeline": [
            {
                "type": "word",
                "text": word["text"],
                "startTime": word["start"],
                "endTime": word["end"],
                "startOffsetUtf16": word["start_offset"],
                "endOffsetUtf16": word["end_offset"],
                "timeline": [],
            }
            for word in words
        ],
    }


def _write_wordtimeline_file(transcriptions_dir: Path, filename: str, text: str = "hello world"):
    (transcriptions_dir / filename).write_text(
        json.dumps({"transcript": text, "wordTimeline": []}),
        encoding="utf-8",
    )


def _write_storyteller_fixture(base_dir: Path):
    transcript_dir = base_dir / "storyteller"
    transcript_dir.mkdir(parents=True, exist_ok=True)

    chapter1 = _chapter_payload(
        "hello world",
        [
            {"text": "hello", "start": 0.5, "end": 0.9, "start_offset": 0, "end_offset": 5},
            {"text": "world", "start": 1.0, "end": 1.4, "start_offset": 6, "end_offset": 11},
        ],
    )
    chapter2 = _chapter_payload(
        "second chapter",
        [
            {"text": "second", "start": 0.2, "end": 0.6, "start_offset": 0, "end_offset": 6},
            {"text": "chapter", "start": 1.2, "end": 1.6, "start_offset": 7, "end_offset": 14},
        ],
    )

    chapter1_name = "00000-00001.json"
    chapter2_name = "00000-00002.json"
    (transcript_dir / chapter1_name).write_text(json.dumps(chapter1), encoding="utf-8")
    (transcript_dir / chapter2_name).write_text(json.dumps(chapter2), encoding="utf-8")

    manifest = {
        "format": "storyteller_manifest",
        "version": 1,
        "duration": 20.0,
        "chapters": [
            {"index": 0, "file": chapter1_name, "start": 0.0, "end": 10.0},
            {"index": 1, "file": chapter2_name, "start": 10.0, "end": 20.0},
        ],
    }
    manifest_path = transcript_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, chapter1, chapter2


def test_storyteller_filename_mapping():
    assert _storyteller_filename_for_abs_chapter(0) == "00000-00001.json"
    assert _storyteller_filename_for_abs_chapter(1) == "00000-00002.json"
    assert _storyteller_filename_for_abs_chapter(9) == "00000-00010.json"


def test_storyteller_validation_accepts_00000_prefix(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_00000"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00000-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00000-00002.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir, 2)
    assert is_valid is True
    assert source_files == ["00000-00001.json", "00000-00002.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_accepts_00001_prefix(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)

    for idx in range(2):
        _write_wordtimeline_file(transcriptions_dir, f"00001-{idx + 1:05d}.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir, 2)
    assert is_valid is True
    assert source_files == ["00001-00001.json", "00001-00002.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_accepts_chapter_first_zero_based(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_chapter_first_zero_based"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00000-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir, 2)
    assert is_valid is True
    assert source_files == ["00000-00001.json", "00001-00001.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_accepts_chapter_first_one_based(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_chapter_first_one_based"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir, 2)
    assert is_valid is True
    assert source_files == ["00001-00001.json", "00002-00001.json"]
    assert destination_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_validation_rejects_count_mismatch(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_count_mismatch"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json")
    _write_wordtimeline_file(transcriptions_dir, "00003-00001.json")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir, 2)
    assert is_valid is False
    assert source_files == []
    assert destination_files == []


def test_storyteller_validation_rejects_non_wordtimeline_format(tmp_path):
    transcriptions_dir = tmp_path / "transcriptions_invalid"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)

    segment_like = [{"start": 0.0, "end": 1.0, "text": "hello"}]
    (transcriptions_dir / "00001-00001.json").write_text(json.dumps(segment_like), encoding="utf-8")

    is_valid, source_files, destination_files = _validate_storyteller_chapters(transcriptions_dir, 1)
    assert is_valid is False
    assert source_files == []
    assert destination_files == []


def test_storyteller_ingest_rewrites_chapter_first_layout_to_canonical(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    transcriptions_dir = assets_root / "assets" / "Book One" / "transcriptions"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json", text="chapter one")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json", text="chapter two")

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    manifest_path = ingest_storyteller_transcripts(
        "abs-ingest-1",
        "Book One",
        [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 20.0}],
    )

    assert manifest_path is not None
    manifest_file = Path(manifest_path)
    assert manifest_file.exists()
    target_dir = data_dir / "transcripts" / "storyteller" / "abs-ingest-1"
    assert (target_dir / "00000-00001.json").exists()
    assert (target_dir / "00000-00002.json").exists()
    assert not (target_dir / "00001-00001.json").exists()
    assert not (target_dir / "00002-00001.json").exists()

    manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    chapter_files = [chapter["file"] for chapter in manifest_payload.get("chapters", [])]
    assert chapter_files == ["00000-00001.json", "00000-00002.json"]


def test_storyteller_ingest_removes_stale_canonical_files(tmp_path, monkeypatch):
    assets_root = tmp_path / "storyteller_assets"
    data_dir = tmp_path / "data"
    transcriptions_dir = assets_root / "assets" / "Book Two" / "transcriptions"
    transcriptions_dir.mkdir(parents=True, exist_ok=True)
    _write_wordtimeline_file(transcriptions_dir, "00001-00001.json", text="chapter one")
    _write_wordtimeline_file(transcriptions_dir, "00002-00001.json", text="chapter two")
    _write_wordtimeline_file(transcriptions_dir, "00003-00001.json", text="chapter three")

    monkeypatch.setenv("STORYTELLER_ASSETS_DIR", str(assets_root))
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    first_manifest = ingest_storyteller_transcripts(
        "abs-ingest-2",
        "Book Two",
        [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 20.0}, {"start": 20.0, "end": 30.0}],
    )
    assert first_manifest is not None

    (transcriptions_dir / "00003-00001.json").unlink()
    second_manifest = ingest_storyteller_transcripts(
        "abs-ingest-2",
        "Book Two",
        [{"start": 0.0, "end": 10.0}, {"start": 10.0, "end": 20.0}],
    )
    assert second_manifest is not None

    target_dir = data_dir / "transcripts" / "storyteller" / "abs-ingest-2"
    assert (target_dir / "00000-00001.json").exists()
    assert (target_dir / "00000-00002.json").exists()
    assert not (target_dir / "00000-00003.json").exists()


def test_transcriber_format_dispatch(tmp_path):
    transcriber = AudioTranscriber(tmp_path, MagicMock(), MagicMock())

    storyteller_chapter = {"transcript": "abc", "wordTimeline": []}
    segment_list = [{"start": 0.0, "end": 1.0, "text": "abc"}]

    assert transcriber._detect_transcript_format(storyteller_chapter) == "storyteller_word_timeline"
    assert transcriber._detect_transcript_format(segment_list) == "segment_list"

    manifest_path, _, _ = _write_storyteller_fixture(tmp_path)
    loaded = transcriber._get_cached_transcript(manifest_path)
    assert isinstance(loaded, StorytellerTranscript)


def test_storyteller_transcript_binary_search_methods(tmp_path):
    manifest_path, _, _ = _write_storyteller_fixture(tmp_path)
    transcript = StorytellerTranscript(manifest_path)

    assert transcript.timestamp_to_char_offset(0.7, chapter_index=0) == 0
    assert transcript.timestamp_to_char_offset(1.1, chapter_index=0) == 6

    assert transcript.char_offset_to_timestamp(0, chapter_index=1) == 0.2
    assert transcript.char_offset_to_timestamp(8, chapter_index=1) == 1.2

    text_at_time = transcript.get_text_at_time(10.3)
    assert "second chapter" in text_at_time

    text_at_offset = transcript.get_text_at_character_offset(7, chapter_index=1)
    assert "second chapter" in text_at_offset


def test_storyteller_utf16_to_python_offset_conversion(tmp_path):
    transcript_dir = tmp_path / "storyteller_utf16"
    transcript_dir.mkdir(parents=True, exist_ok=True)

    chapter_name = "00000-00001.json"
    chapter_payload = {
        "transcript": "A🙂B",
        "wordTimeline": [
            {
                "type": "word",
                "text": "A",
                "startTime": 0.1,
                "endTime": 0.2,
                "startOffsetUtf16": 0,
                "endOffsetUtf16": 1,
                "timeline": [],
            },
            {
                "type": "word",
                "text": "🙂",
                "startTime": 0.3,
                "endTime": 0.4,
                "startOffsetUtf16": 1,
                "endOffsetUtf16": 3,
                "timeline": [],
            },
            {
                "type": "word",
                "text": "B",
                "startTime": 0.5,
                "endTime": 0.6,
                "startOffsetUtf16": 3,
                "endOffsetUtf16": 4,
                "timeline": [],
            },
        ],
    }
    (transcript_dir / chapter_name).write_text(json.dumps(chapter_payload), encoding="utf-8")

    manifest = {
        "format": "storyteller_manifest",
        "version": 1,
        "duration": 1.0,
        "chapters": [
            {"index": 0, "file": chapter_name, "start": 0.0, "end": 1.0, "text_len": 3, "text_len_utf16": 4},
        ],
    }
    manifest_path = transcript_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    transcript = StorytellerTranscript(manifest_path)
    assert transcript.chapter_utf16_to_python_offset(0, 1) == 1
    assert transcript.chapter_utf16_to_python_offset(0, 3) == 2

    story_pos = transcript.timestamp_to_story_position(0.35)
    assert story_pos is not None
    assert story_pos["offset_utf16"] == 1
    assert story_pos["offset_py"] == 1
    assert story_pos["global_offset_py"] == 1
