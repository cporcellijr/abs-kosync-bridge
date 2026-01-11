import json
import logging
import os
from typing import Optional

from json_db import JsonDB
from src.sync_clients.sync_client_interface import SyncClient, LocatorResult, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)


class ABSSyncClient(SyncClient):
    def __init__(self, abs_client, transcriber, ebook_parser, db_handler: JsonDB):
        super().__init__(ebook_parser)
        self.abs_client = abs_client
        self.transcriber = transcriber
        self.db_handler = db_handler
        self.abs_progress_offset = float(os.getenv("ABS_PROGRESS_OFFSET_SECONDS", 0))
        self.delta_abs_thresh = float(os.getenv("SYNC_DELTA_ABS_SECONDS", 60))

    def is_configured(self) -> bool:
        # ABS is always considered configured (it's the primary service)
        return True

    def get_service_state(self, mapping: dict, prev: dict, title_snip: str = "") -> ServiceState:
        abs_id = mapping['abs_id']
        abs_ts = self.abs_client.get_progress(abs_id)

        if abs_ts is None:
            # Return a state indicating ABS is offline
            return ServiceState(
                current={'pct': 0.0, 'ts': None},
                previous_pct=prev.get('abs_pct', 0),
                delta=0.0,
                threshold=self.delta_abs_thresh,
                is_configured=True,
                display=("ABS", "Offline"),
                value_seconds_formatter=lambda v: f"{v:.2f}s",
                value_formatter=lambda v: f"{v:.4%}"
            )

        # Convert timestamp to percentage
        abs_pct = self._abs_to_percentage(abs_ts, mapping.get('transcript_file'))
        if abs_ts > 0 and abs_pct is None:
            # Invalid transcript
            return ServiceState(
                current={'pct': 0.0, 'ts': abs_ts},
                previous_pct=prev.get('abs_pct', 0),
                delta=0.0,
                threshold=self.delta_abs_thresh,
                is_configured=True,
                display=("ABS", "Invalid transcript"),
                value_seconds_formatter=lambda v: f"{v:.2f}s",
                value_formatter=lambda v: f"{v:.4%}"
            )

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
            delta = ts_for_text - request.previous_location
            result, final_pct = self._update_abs_progress_with_offset(mapping['abs_id'], ts_for_text, delta, mapping.get('abs_session_id'))
            return SyncResult(final_pct, result.get("success", False))
        return SyncResult(None, False)

    def _store_abs_session_id(self, abs_id, session_id):
        db = self.db_handler.load()
        updated = False
        for mapping in db.get('mappings', []):
            if mapping.get('abs_id') == abs_id:
                mapping['abs_session_id'] = session_id
                updated = True
        if updated:
            self.db_handler.save(db)
            logger.info(f"ABS sessionId saved for {abs_id}: {session_id}")

    def _update_abs_progress_with_offset(self, abs_id, ts, delta, session_id=None):
        """Apply offset to timestamp and update ABS progress using the new session-based sync endpoint."""
        adjusted_ts = max(round(ts + self.abs_progress_offset, 2), 0)
        if self.abs_progress_offset != 0:
            logger.debug(f"   📐 Adjusted timestamp: {ts}s → {adjusted_ts}s (offset: {self.abs_progress_offset:+.1f}s)")

        # If there is no current session, create a new session
        if not session_id:
            session_id = self.abs_client.create_session(abs_id)
            if session_id:
                self._store_abs_session_id(abs_id, session_id)

        # Now update progress using the sessionId
        abs_result = {"success": False, "code": None, "response": None}
        if session_id:
            time_listened = max(0, min(delta, 600))
            abs_result = self.abs_client.update_progress(session_id, adjusted_ts, time_listened)
            # Retry logic for 404: create new session and try again
            if abs_result.get("code") == 404:
                logger.warning(f"ABS sessionId {session_id} not found (404). Attempting to create a new session and retry progress update.")
                new_session_id = self.abs_client.create_session(abs_id)
                if new_session_id:
                    self._store_abs_session_id(abs_id, new_session_id)
                    abs_result = self.abs_client.update_progress(new_session_id, adjusted_ts, time_listened)
                else:
                    logger.error("Failed to create new ABS session for retry after 404.")
        else:
            logger.error("No sessionId available for ABS progress update.")
        return abs_result, adjusted_ts
