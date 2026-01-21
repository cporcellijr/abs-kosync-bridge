import os
from typing import Optional
import logging

from src.api.booklore_client import BookloreClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState

logger = logging.getLogger(__name__)

class BookloreSyncClient(SyncClient):
    def __init__(self, booklore_client: BookloreClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.booklore_client = booklore_client
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.booklore_client.is_configured()

    def check_connection(self):
        return self.booklore_client.check_connection()

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        epub = book.ebook_filename
        bl_pct, _ = self.booklore_client.get_progress(epub)

        if bl_pct is None:
            logger.warning("⚠️ BookLore percentage is None - returning None for service state")
            return None

        # Get previous BookLore state
        prev_booklore_pct = prev_state.percentage if prev_state else 0

        delta = abs(bl_pct - prev_booklore_pct)

        return ServiceState(
            current={"pct": bl_pct},
            previous_pct=prev_booklore_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.booklore_client.is_configured(),
            display=("BookLore", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        bl_pct = state.current.get('pct')
        epub = book.ebook_filename
        if bl_pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, bl_pct)
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        epub = book.ebook_filename
        pct = request.locator_result.percentage
        success = self.booklore_client.update_progress(epub, pct, request.locator_result)
        updated_state = {
            'pct': pct
        }
        return SyncResult(pct, success, updated_state)
