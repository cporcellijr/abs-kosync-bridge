import json
import logging
import os
from typing import Optional

from src.api.api_clients import ABSClient
from src.utils.ebook_utils import EbookParser
from src.db.json_db import JsonDB
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.transcriber import AudioTranscriber

logger = logging.getLogger(__name__)

class ABSSyncClient(SyncClient):
    def __init__(self, abs_client: ABSClient, transcriber: AudioTranscriber, ebook_parser: EbookParser, db_handler: JsonDB):
        super().__init__(ebook_parser)
        self.abs_client = abs_client
        self.transcriber = transcriber
        self.db_handler = db_handler
        self.abs_progress_offset = float(os.getenv("ABS_PROGRESS_OFFSET_SECONDS", 0))
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))

    def is_configured(self) -> bool:
        # ABS is always considered configured (it's the primary service)
        return True

    def get_service_state(self, mapping: dict, prev: dict, title_snip: str = "") -> Optional[ServiceState]:
        abs_id = mapping['abs_id']
        response = self.abs_client.get_progress(abs_id)
        abs_ts = response.get('currentTime') if response is not None else None

        if abs_ts is None:
            logger.info("ABS timestamp is None, probably not started the book yet.")
            abs_ts = 0.0

        # Convert timestamp to percentage
        abs_pct = self._abs_to_percentage(abs_ts, mapping.get('transcript_file'))
        if abs_ts > 0 and abs_pct is None:
            # Invalid transcript
            logger.warning("⚠️ Unable to convert ABS timestamp to percentage - invalid transcript?")
            return None

        prev_abs_ts = prev.get('abs_ts', 0)
        prev_abs_pct = prev.get('abs_pct', 0)

        delta = abs(abs_ts - prev_abs_ts) if abs_ts and prev_abs_ts else abs(abs_ts - prev_abs_ts) if abs_ts else 0

        return ServiceState(
            current={'pct': abs_pct, 'ts': abs_ts},
            previous_pct=prev_abs_pct,
            delta=delta,
            threshold=self.delta_abs_thresh,
            is_configured=True,
            display=("ABS", "{prev:.4%} -> {curr:.4%}"),
            value_seconds_formatter=lambda v: f"{v:.2f}s",
            value_formatter=lambda v: f"{v:.4%}"
        )

    def _abs_to_percentage(self, abs_seconds, transcript_path):
        """Convert ABS timestamp to percentage using transcript duration"""
        try:
            with open(transcript_path, 'r') as f:
                data = json.load(f)
                dur = data[-1]['end'] if isinstance(data, list) else data.get('duration', 0)
                return min(max(abs_seconds / dur, 0.0), 1.0) if dur > 0 else None
        except:
            return None

    def get_text_from_current_state(self, mapping: dict, state: ServiceState) -> Optional[str]:
        abs_ts = state.current.get('ts')
        if not mapping or abs_ts is None:
            return None
        return self.transcriber.get_text_at_time(mapping.get('transcript_file'), abs_ts)

    def update_progress(self, mapping: dict, request: UpdateProgressRequest) -> SyncResult:
        book_title = mapping.get('abs_title', 'Unknown Book')
        ts_for_text = self.transcriber.find_time_for_text(mapping.get('transcript_file'), request.txt,
                                                          hint_percentage=request.locator_result.percentage,
                                                          book_title=book_title)
        if ts_for_text is not None:
            result, final_ts = self._update_abs_progress_with_offset(mapping['abs_id'], ts_for_text, request.previous_location)
            # Calculate percentage from timestamp for state
            pct = self._abs_to_percentage(final_ts, mapping.get('transcript_file'))
            updated_state = {
                'ts': final_ts,
                'pct': pct or 0
            }
            return SyncResult(final_ts, result.get("success", False), updated_state)
        logger.warning(f"[{book_title}] Not updating ABS progress - could not find timestamp for provided text.")
        return SyncResult(None, False)

    def _update_abs_progress_with_offset(self, abs_id, ts, prev_abs_ts: float =0):
        """Apply offset to timestamp and update ABS progress.

        Args:
            abs_id: ABS library item ID
            ts: New timestamp to set (seconds)
            prev_abs_ts: Previous ABS timestamp for calculating time_listened
        """
        adjusted_ts = max(round(ts + self.abs_progress_offset, 2), 0)
        if self.abs_progress_offset != 0:
            logger.debug(f"   📐 Adjusted timestamp: {ts}s → {adjusted_ts}s (offset: {self.abs_progress_offset:+.1f}s)")

        # Calculate time_listened as the difference between new and previous position
        time_listened = max(0, adjusted_ts - prev_abs_ts)

        # Don't send negative time_listened (shouldn't happen, but safety check)
        if time_listened < 0:
            time_listened = 0

        logger.debug(f"   ⏱️ time_listened: {time_listened:.1f}s (prev: {prev_abs_ts:.1f}s → new: {adjusted_ts:.1f}s)")
        abs_ok = self.abs_client.update_progress(abs_id, adjusted_ts, time_listened)
        return abs_ok, adjusted_ts
