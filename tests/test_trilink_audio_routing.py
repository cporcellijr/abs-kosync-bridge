from types import SimpleNamespace
from unittest.mock import MagicMock

from src.sync_clients.abs_sync_client import ABSSyncClient
from src.sync_clients.booklore_audio_sync_client import BookLoreAudioSyncClient
from src.sync_clients.sync_client_interface import LocatorResult, UpdateProgressRequest
from src.sync_manager import SyncManager


def test_abs_sync_client_prefers_seek_timestamp():
    abs_client = MagicMock()
    abs_client.get_progress.return_value = {"currentTime": 10.0}
    abs_client.update_progress.return_value = {"success": True}

    client = ABSSyncClient(abs_client=abs_client, transcriber=MagicMock(), ebook_parser=MagicMock(), alignment_service=MagicMock())
    book = SimpleNamespace(abs_id="abs-1", abs_title="Book", duration=200.0, transcript_file="DB_MANAGED")
    req = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.3), txt=None, seek_timestamp=50.0)

    result = client.update_progress(book, req)

    assert result.success is True
    assert result.updated_state["ts"] == 50.0
    abs_client.update_progress.assert_called_once()
    call = abs_client.update_progress.call_args
    assert call.args[1] == 50.0


def test_booklore_audio_prefers_seek_timestamp_for_resume_fields():
    bl_client = MagicMock()
    bl_client.get_audiobook_info.return_value = {
        "bookFileId": 123,
        "folderBased": True,
        "tracks": [
            {"index": 0, "durationMs": 30000, "cumulativeStartMs": 0},
            {"index": 1, "durationMs": 30000, "cumulativeStartMs": 30000},
        ],
    }
    bl_client.update_audiobook_progress.return_value = True

    client = BookLoreAudioSyncClient(bl_client, MagicMock(), alignment_service=MagicMock())
    book = SimpleNamespace(
        abs_id="abs-1",
        abs_title="Book",
        audio_source="BookLore",
        audio_source_id="bl-1",
        audio_duration=60.0,
        duration=60.0,
        transcript_file="DB_MANAGED",
    )
    req = UpdateProgressRequest(locator_result=LocatorResult(percentage=0.1), seek_timestamp=40.0)

    result = client.update_progress(book, req)

    assert result.success is True
    kwargs = bl_client.update_audiobook_progress.call_args.kwargs
    assert kwargs["position_ms"] == 10000
    assert kwargs["track_index"] == 1


def test_sync_manager_resolve_locator_char_offset_fallbacks():
    manager = SyncManager.__new__(SyncManager)
    manager.ebook_parser = MagicMock()

    book = SimpleNamespace(ebook_filename="book.epub", original_ebook_filename=None)
    loc = LocatorResult(percentage=0.2, xpath="/x")

    manager.ebook_parser.resolve_xpath_to_index.return_value = 123
    offset, source = manager._resolve_locator_char_offset(book, loc, "book.epub")

    assert offset == 123
    assert source == "xpath"
