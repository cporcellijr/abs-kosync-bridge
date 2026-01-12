import os
from typing import Optional
import logging

from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
logger = logging.getLogger(__name__)

class StorytellerSyncClient(SyncClient):
    def __init__(self, storyteller_db, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.storyteller_db = storyteller_db
        self.ebook_parser = ebook_parser
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.storyteller_db.is_configured()

    def get_service_state(self, mapping: dict, prev: dict, title_snip: str = "") -> Optional[ServiceState]:
        epub = mapping['ebook_filename']
        st_pct, st_ts, st_href, st_frag = self.storyteller_db.get_progress_with_fragment(epub)

        if st_pct is None:
            logger.warning("⚠️ Storyteller percentage is None - returning None for service state")
            return None

        prev_storyteller_pct = prev.get('storyteller_pct', 0)
        delta = abs(st_pct - prev_storyteller_pct)

        return ServiceState(
            current={"pct": st_pct, "ts": st_ts, "href": st_href, "frag": st_frag},
            previous_pct=prev_storyteller_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.storyteller_db.is_configured(),
            display=("Storyteller", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, mapping: dict, state: ServiceState) -> Optional[str]:
        # This needs to be updated to work with the new interface
        epub = mapping.get('ebook_filename')
        st_pct, href, frag = state.current.get('pct'), state.current.get('href'), state.current.get('frag')
        txt = self.ebook_parser.resolve_locator_id(epub, href, frag)
        if not txt:
            txt = self.ebook_parser.get_text_at_percentage(epub, st_pct)
        return txt

    def update_progress(self, mapping: dict, request: UpdateProgressRequest) -> SyncResult:
        epub = mapping['ebook_filename']
        pct = request.locator_result.percentage
        locator = request.locator_result
        return SyncResult(pct, self.storyteller_db.update_progress(epub, pct, locator))

