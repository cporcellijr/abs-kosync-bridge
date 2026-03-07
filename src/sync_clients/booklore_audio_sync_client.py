import logging
import os
from typing import Optional

from src.api.booklore_client import BookloreClient
from src.db.models import Book, State
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser

logger = logging.getLogger(__name__)


class BookLoreAudioSyncClient(SyncClient):
    def __init__(self, booklore_client: BookloreClient, ebook_parser: EbookParser, alignment_service=None):
        super().__init__(ebook_parser)
        self.booklore_client = booklore_client
        self.alignment_service = alignment_service
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))

    def is_configured(self) -> bool:
        return self.booklore_client.is_configured()

    def check_connection(self):
        return self.booklore_client.check_connection()

    def get_supported_sync_types(self) -> set:
        return {"audiobook"}

    def supports_book(self, book: Book) -> bool:
        return getattr(book, "audio_source", None) == "BookLore"

    def _resolve_booklore_book_id(self, book: Book) -> Optional[str]:
        return (
            getattr(book, "audio_provider_book_id", None)
            or getattr(book, "audio_source_id", None)
        )

    def _resolve_booklore_file_id(self, book: Book) -> Optional[str]:
        file_id = getattr(book, "audio_provider_file_id", None)
        if file_id:
            return str(file_id)
        book_id = self._resolve_booklore_book_id(book)
        if not book_id:
            return None
        info = self.booklore_client.get_audiobook_info(book_id) or {}
        fetched = info.get("bookFileId")
        return str(fetched) if fetched is not None else None

    def _get_duration_seconds(self, book: Book) -> Optional[float]:
        for attr in ("audio_duration", "duration"):
            value = getattr(book, attr, None)
            try:
                if value is not None and float(value) > 0:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def get_service_state(
        self,
        book: Book,
        prev_state: Optional[State],
        title_snip: str = "",
        bulk_context: dict = None,
    ) -> Optional[ServiceState]:
        book_id = self._resolve_booklore_book_id(book)
        if not book_id:
            return None

        progress = self.booklore_client.get_audiobook_progress(book_id)
        if progress is None:
            return None

        current_pct = progress.get("pct")
        position_ms = progress.get("position_ms")
        current_ts = float(position_ms) / 1000.0 if position_ms is not None else None
        duration = self._get_duration_seconds(book)
        if current_pct is None and current_ts is not None and duration:
            current_pct = min(max(current_ts / duration, 0.0), 1.0)
        if current_pct is None:
            current_pct = 0.0
        if current_ts is None and duration is not None:
            current_ts = current_pct * duration

        prev_ts = prev_state.timestamp if prev_state and prev_state.timestamp is not None else 0.0
        prev_pct = prev_state.percentage if prev_state and prev_state.percentage is not None else 0.0
        delta = abs((current_ts or 0.0) - prev_ts)

        return ServiceState(
            current={"pct": current_pct, "ts": current_ts},
            previous_pct=prev_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=self.booklore_client.is_configured(),
            display=("BookLoreAudio", "{prev:.4%} -> {curr:.4%}"),
            value_seconds_formatter=lambda v: f"{v:.2f}s",
            value_formatter=lambda v: f"{v:.4%}",
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState):
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        book_id = self._resolve_booklore_book_id(book)
        if not book_id:
            return SyncResult(None, False)

        if request.locator_result.percentage == 0.0:
            success = self.booklore_client.update_audiobook_progress(
                book_id=book_id,
                book_file_id=self._resolve_booklore_file_id(book),
                position_ms=0,
                percentage=0.0,
            )
            updated_state = {"pct": 0.0, "ts": 0.0}
            if success:
                try:
                    from src.services.write_tracker import record_write

                    record_write("BookLoreAudio", book.abs_id, 0.0)
                except ImportError:
                    pass
            return SyncResult(0.0, success, updated_state)

        target_ts = None
        if book.transcript_file == "DB_MANAGED" and self.alignment_service and request.txt:
            target_ts = self.alignment_service.get_time_for_text(
                book.abs_id,
                request.txt,
                char_offset_hint=request.locator_result.match_index,
            )

        if target_ts is None:
            duration = self._get_duration_seconds(book)
            if duration:
                target_ts = max(0.0, min(duration, request.locator_result.percentage * duration))

        if target_ts is None:
            logger.warning(
                "BookLoreAudio: cannot update '%s' because no target timestamp could be resolved",
                getattr(book, "abs_title", book.abs_id),
            )
            return SyncResult(None, False)

        position_ms = int(round(target_ts * 1000.0))
        percentage = request.locator_result.percentage
        success = self.booklore_client.update_audiobook_progress(
            book_id=book_id,
            book_file_id=self._resolve_booklore_file_id(book),
            position_ms=position_ms,
            percentage=percentage,
        )
        if success:
            try:
                from src.services.write_tracker import record_write

                record_write("BookLoreAudio", book.abs_id, percentage)
            except ImportError:
                pass
        return SyncResult(
            target_ts,
            success,
            {
                "pct": percentage,
                "ts": target_ts,
            },
        )
