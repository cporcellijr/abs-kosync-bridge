from unittest.mock import MagicMock, patch

from src.db.models import Book
from src.sync_clients.booklore_audio_sync_client import BookLoreAudioSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest


def _make_book(**overrides):
    values = {
        "abs_id": "abs-1",
        "abs_title": "BookLore Audio Test",
        "audio_source": "BookLore",
        "audio_source_id": "bl-1",
        "audio_duration": 100.0,
        "duration": 100.0,
        "ebook_filename": "test.epub",
        "status": "active",
    }
    values.update(overrides)
    return Book(**values)


def _folder_based_info():
    return {
        "bookFileId": 10157,
        "folderBased": True,
        "tracks": [
            {"index": 0, "durationMs": 30000, "cumulativeStartMs": 0},
            {"index": 1, "durationMs": 40000, "cumulativeStartMs": 30000},
            {"index": 2, "durationMs": 30000, "cumulativeStartMs": 70000},
        ],
    }


def test_get_service_state_reconstructs_absolute_timestamp_from_track_progress():
    booklore_client = MagicMock()
    booklore_client.get_audiobook_progress.return_value = {
        "pct": 0.5,
        "position_ms": 5000,
        "track_index": 1,
    }
    booklore_client.get_audiobook_info.return_value = _folder_based_info()
    booklore_client.is_configured.return_value = True

    client = BookLoreAudioSyncClient(booklore_client, MagicMock())
    state = client.get_service_state(_make_book(), None)

    assert state is not None
    assert state.current["pct"] == 0.5
    assert state.current["ts"] == 35.0
    booklore_client.get_audiobook_info.assert_called_once_with("bl-1")


def test_update_progress_converts_absolute_target_to_track_relative_resume_fields():
    booklore_client = MagicMock()
    booklore_client.get_audiobook_info.return_value = _folder_based_info()
    booklore_client.update_audiobook_progress.return_value = True

    client = BookLoreAudioSyncClient(booklore_client, MagicMock())
    book = _make_book()
    request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.5))

    with patch("src.services.write_tracker.record_write"):
        result = client.update_progress(book, request)

    assert result.success is True
    assert result.location == 50.0
    kwargs = booklore_client.update_audiobook_progress.call_args.kwargs
    assert kwargs["book_file_id"] == "10157"
    assert kwargs["position_ms"] == 20000
    assert kwargs["track_index"] == 1
    assert kwargs["track_position_ms"] == 20000
    booklore_client.get_audiobook_info.assert_called_once_with("bl-1")


def test_update_progress_clamps_end_of_book_to_final_track():
    booklore_client = MagicMock()
    booklore_client.get_audiobook_info.return_value = _folder_based_info()
    booklore_client.update_audiobook_progress.return_value = True

    client = BookLoreAudioSyncClient(booklore_client, MagicMock())
    request = UpdateProgressRequest(locator_result=LocatorResult(percentage=1.0))

    with patch("src.services.write_tracker.record_write"):
        result = client.update_progress(_make_book(), request)

    assert result.success is True
    kwargs = booklore_client.update_audiobook_progress.call_args.kwargs
    assert kwargs["position_ms"] == 30000
    assert kwargs["track_index"] == 2
    assert kwargs["track_position_ms"] == 30000


def test_get_service_state_falls_back_to_percentage_when_track_metadata_is_missing():
    booklore_client = MagicMock()
    booklore_client.get_audiobook_progress.return_value = {
        "pct": 0.25,
        "position_ms": 5000,
        "track_index": 1,
    }
    booklore_client.get_audiobook_info.return_value = None
    booklore_client.is_configured.return_value = True

    client = BookLoreAudioSyncClient(booklore_client, MagicMock())
    state = client.get_service_state(_make_book(audio_duration=100.0, duration=100.0), None)

    assert state is not None
    assert state.current["pct"] == 0.25
    assert state.current["ts"] == 25.0


def test_update_progress_zero_reset_writes_start_of_first_track():
    booklore_client = MagicMock()
    booklore_client.get_audiobook_info.return_value = _folder_based_info()
    booklore_client.update_audiobook_progress.return_value = True

    client = BookLoreAudioSyncClient(booklore_client, MagicMock())
    request = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.0))

    with patch("src.services.write_tracker.record_write"):
        result = client.update_progress(_make_book(), request)

    assert result.success is True
    assert result.location == 0.0
    kwargs = booklore_client.update_audiobook_progress.call_args.kwargs
    assert kwargs["position_ms"] == 0
    assert kwargs["track_index"] == 0
    assert kwargs["track_position_ms"] == 0
