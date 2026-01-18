import os
from typing import Optional
import logging

from src.api.api_clients import KoSyncClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)

class KoSyncSyncClient(SyncClient):
    def __init__(self, kosync_client: KoSyncClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.kosync_client = kosync_client
        self.ebook_parser = ebook_parser
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.kosync_client.is_configured()

    def check_connection(self):
        return self.kosync_client.check_connection()

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "") -> Optional[ServiceState]:
        ko_id = book.kosync_doc_id
        ko_pct, ko_xpath = self.kosync_client.get_progress(ko_id)
        if ko_xpath is None:
            logger.debug(f"⚠️ [{title_snip}] KoSync xpath is None - will use fallback text extraction")

        if ko_pct is None:
            logger.warning("⚠️ KoSync percentage is None - returning None for service state")
            return None

        # Get previous KoSync state
        prev_kosync_pct = prev_state.percentage if prev_state else 0

        delta = abs(ko_pct - prev_kosync_pct)

        return ServiceState(
            current={"pct": ko_pct, "xpath": ko_xpath},
            previous_pct=prev_kosync_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.kosync_client.is_configured(),
            display=("KoSync", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        ko_xpath = state.current.get('xpath')
        ko_pct = state.current.get('pct')
        epub = book.ebook_filename
        if ko_xpath and epub:
            txt = self.ebook_parser.resolve_xpath(epub, ko_xpath)
            if txt:
                return txt
        if ko_pct is not None and epub:
            return self.ebook_parser.get_text_at_percentage(epub, ko_pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        pct = request.locator_result.percentage
        locator = request.locator_result
        ko_id = book.kosync_doc_id if book else None
        # use perfect_ko_xpath if available
        xpath = locator.perfect_ko_xpath if locator and locator.perfect_ko_xpath else locator.xpath
        success = self.kosync_client.update_progress(ko_id, pct, xpath)
        updated_state = {
            'pct': pct,
            'xpath': xpath
        }
        return SyncResult(pct, success, updated_state)

