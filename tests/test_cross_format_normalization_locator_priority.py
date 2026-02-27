import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

# Match existing tests that add project root for `src.*` imports.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sync_clients.booklore_sync_client import BookloreSyncClient
from src.sync_clients.sync_client_interface import ServiceState
from src.sync_manager import SyncManager
from src.utils.ebook_utils import EbookParser


def _state(current: dict) -> ServiceState:
    return ServiceState(
        current=current,
        previous_pct=0.0,
        delta=0.0,
        threshold=0.01,
        is_configured=True,
        display=("X", "{prev:.2%}->{curr:.2%}"),
        value_formatter=lambda v: f"{v:.4%}",
    )


def _manager_with_mocks():
    manager = SyncManager.__new__(SyncManager)
    manager.ebook_parser = MagicMock()
    manager.alignment_service = MagicMock()
    manager.sync_clients = {
        "ABS": object(),
        "KoSync": object(),
        "BookLore": object(),
    }
    return manager


def test_normalization_prefers_xpath_offset():
    manager = _manager_with_mocks()
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager.ebook_parser.resolve_xpath_to_index.return_value = 123
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.alignment_service.get_time_for_text.return_value = 555.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "KoSync": _state({"pct": 0.5, "xpath": "/body/DocFragment[1]/body/p[1]/text().0"}),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["KoSync"] == 555.0
    _, kwargs = manager.alignment_service.get_time_for_text.call_args
    assert kwargs["char_offset_hint"] == 123
    manager.ebook_parser.resolve_xpath_to_index.assert_called_once()


def test_normalization_prefers_cfi_before_percent():
    manager = _manager_with_mocks()
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = 321
    manager.alignment_service.get_time_for_text.return_value = 777.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state({"pct": 0.4, "cfi": "epubcfi(/6/10!/4:0)"}),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["BookLore"] == 777.0
    _, kwargs = manager.alignment_service.get_time_for_text.call_args
    assert kwargs["char_offset_hint"] == 321
    manager.ebook_parser.resolve_cfi_to_index.assert_called_once()


def test_normalization_falls_back_to_percent_when_no_locator():
    manager = _manager_with_mocks()
    manager.ebook_parser.resolve_book_path.return_value = "book.epub"
    manager.ebook_parser.extract_text_and_map.return_value = ("a" * 1000, [])
    manager.ebook_parser.resolve_xpath_to_index.return_value = None
    manager.ebook_parser.resolve_cfi_to_index.return_value = None
    manager.alignment_service.get_time_for_text.return_value = 888.0

    book = SimpleNamespace(abs_id="abs-1", transcript_file="DB_MANAGED", ebook_filename="book.epub")
    config = {
        "ABS": _state({"ts": 10.0}),
        "BookLore": _state({"pct": 0.4}),
    }

    normalized = manager._normalize_for_cross_format_comparison(book, config)

    assert normalized["BookLore"] == 888.0
    _, kwargs = manager.alignment_service.get_time_for_text.call_args
    assert kwargs["char_offset_hint"] == 400


def test_determine_leader_uses_locator_pct_when_raw_pct_is_inconsistent():
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {
        "ABS": _Client(),
        "KoSync": _Client(),
        "BookLore": _Client(),
    }
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name in {"KoSync", "BookLore"})
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 4124.7, "KoSync": 4086.2, "BookLore": 4113.3}
    )

    book = SimpleNamespace(duration=10000, transcript_file="DB_MANAGED")
    config = {
        "ABS": _state({"pct": 0.1015, "ts": 4124.7}),
        "KoSync": _state({"pct": 0.104255}),
        "BookLore": _state({"pct": 0.0, "cfi": "epubcfi(/6/16!/4/14:0)", "_locator_pct": 0.1010}),
    }

    leader, leader_pct = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "BookLore"
    assert leader_pct == 0.1010
    assert config["BookLore"].current["pct"] == 0.1010


def test_booklore_get_text_prefers_cfi_over_percentage():
    ebook_parser = MagicMock()
    ebook_parser.get_text_around_cfi.return_value = "cfi text"
    booklore_client = MagicMock()
    client = BookloreSyncClient(booklore_client, ebook_parser)
    state = _state({"pct": 0.0, "cfi": "epubcfi(/6/16!/4/14:0)"})
    book = SimpleNamespace(ebook_filename="book.epub")

    text = client.get_text_from_current_state(book, state)

    assert text == "cfi text"
    ebook_parser.get_text_around_cfi.assert_called_once_with("book.epub", "epubcfi(/6/16!/4/14:0)")
    ebook_parser.get_text_at_percentage.assert_not_called()


def test_determine_leader_ignores_stale_booklore_raw_delta():
    manager = SyncManager.__new__(SyncManager)

    class _Client:
        def can_be_leader(self):
            return True

    manager.sync_clients = {
        "ABS": _Client(),
        "KoSync": _Client(),
        "BookLore": _Client(),
    }
    manager._has_significant_delta = MagicMock(side_effect=lambda name, cfg, book: name in {"KoSync", "BookLore"})
    manager._normalize_for_cross_format_comparison = MagicMock(
        return_value={"ABS": 23404.6, "KoSync": 23379.2, "BookLore": 23397.2}
    )

    book = SimpleNamespace(duration=40556, transcript_file="DB_MANAGED")
    config = {
        "ABS": _state({"pct": 0.5763, "ts": 23404.6}),
        "KoSync": _state({"pct": 0.5894}),
        "BookLore": _state({"pct": 0.2980, "cfi": "epubcfi(/6/46!/4/16:0)", "_locator_pct": 0.5838}),
    }
    config["KoSync"].previous_pct = 0.5838
    config["BookLore"].previous_pct = 0.5838

    leader, _ = manager._determine_leader(config, book, "abs-1", "book")

    assert leader == "KoSync"


def test_parse_cfi_components_supports_minimal_cfi():
    parser = EbookParser.__new__(EbookParser)

    spine_step, element_steps, char_offset = parser._parse_cfi_components("epubcfi(/6/26!/:0)")

    assert spine_step == 26
    assert element_steps == []
    assert char_offset == 0


def test_generate_cfi_never_emits_empty_element_path():
    parser = EbookParser.__new__(EbookParser)

    cfi = parser._generate_cfi(12, "plain text without body wrapper", 1)

    assert "!/:" not in cfi
