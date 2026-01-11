import os
from typing import Optional

from booklore_client import BookloreClient
from ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, LocatorResult, SyncResult, UpdateProgressRequest, ServiceState


class BookloreSyncClient(SyncClient):
    def __init__(self, booklore_client: BookloreClient, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.booklore_client = booklore_client
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.booklore_client.is_configured()

    def get_service_state(self, mapping: dict, prev: dict, title_snip: str = "") -> ServiceState:
        epub = mapping['ebook_filename']
        bl_pct, _ = self.booklore_client.get_progress(epub)

        if bl_pct is None:
            bl_pct = 0.0

        prev_booklore_pct = prev.get('booklore_pct', 0)
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

    def get_text_from_current_state(self, mapping: dict, state: ServiceState) -> Optional[str]:
        bl_pct = state.current.get('pct')
        epub = mapping['ebook_filename']
        if bl_pct is not None and epub and self.ebook_parser:
            return self.ebook_parser.get_text_at_percentage(epub, bl_pct)
        return None

    def update_progress(self, mapping: dict, request: UpdateProgressRequest) -> SyncResult:
        epub = mapping['ebook_filename']
        pct = request.locator_result.percentage
        return SyncResult(pct, self.booklore_client.update_progress(epub, pct, request.locator_result))
