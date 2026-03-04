import os
from typing import Optional
import logging

from src.api.storyteller_api import StorytellerAPIClient
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, LocatorResult, SyncResult, UpdateProgressRequest, ServiceState
logger = logging.getLogger(__name__)

class StorytellerSyncClient(SyncClient):
    def __init__(self, storyteller_client: StorytellerAPIClient, ebook_parser: EbookParser, database_service=None):
        super().__init__(ebook_parser)
        self.storyteller_client = storyteller_client
        self.ebook_parser = ebook_parser
        self.database_service = database_service
        self.delta_kosync_thresh = float(os.getenv("SYNC_DELTA_KOSYNC_PERCENT", 1)) / 100.0

    def is_configured(self) -> bool:
        return self.storyteller_client.is_configured()

    def check_connection(self):
        return self.storyteller_client.check_connection()

    def fetch_bulk_state(self):
        """Pre-fetch all Storyteller progress data at once."""
        return self.storyteller_client.get_all_positions_bulk()

    def get_supported_sync_types(self) -> set:
        """Storyteller participates in both audiobook and ebook sync modes."""
        return {'audiobook', 'ebook'}

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        # [Tri-Link Fix] Strict UUID Sync Only
        uuid = book.storyteller_uuid
        
        if not uuid:
            # Strict mode: If no UUID is linked, Storyteller is effectively disabled for this book.
            # We do NOT fallback to filename search or legacy methods.
            return None

        st_pct, st_ts, st_href, st_frag = None, None, None, None
        
        try:
            st_pct, st_ts, st_href, st_frag = self.storyteller_client.get_position_details(uuid)
        except Exception as e:
            logger.warning(f"⚠️ '{title_snip}' Storyteller UUID fetch failed for '{uuid}': {e}")
            return None

        # Calculate delta
        prev_storyteller_pct = prev_state.percentage if prev_state else 0
        
        # If st_pct is None here, it means the book exists but has no position yet (or fetch failed).
        # We treat it as 0% for calculation if it returned valid None, or bail if it crashed.
        # But get_position_details usually returns None tuple on failure, so we check st_pct.
        if st_pct is None:
             st_pct = 0.0
             st_ts = 0
             delta = 0 # No movement
        else:
             delta = abs(st_pct - prev_storyteller_pct)

        return ServiceState(
            current={"pct": st_pct, "ts": st_ts, "href": st_href, "frag": st_frag},
            previous_pct=prev_storyteller_pct,
            delta=delta,
            threshold=self.delta_kosync_thresh,
            is_configured=self.storyteller_client.is_configured(),
            display=("Storyteller", "{prev:.4%} -> {curr:.4%}"),
            value_formatter=lambda v: f"{v*100:.4f}%"
        )

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        # This needs to be updated to work with the new interface
        epub = book.ebook_filename
        st_pct, href, frag = state.current.get('pct'), state.current.get('href'), state.current.get('frag')
        txt = None
        if href and frag:
            txt = self.ebook_parser.resolve_locator_id(epub, href, frag)
        elif href and not frag:
            logger.debug(f"Storyteller state missing fragment for href='{href}', falling back to percentage text")
        if not txt:
            txt = self.ebook_parser.get_text_at_percentage(epub, st_pct)
        return txt

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        epub = book.ebook_filename
        pct = request.locator_result.percentage
        locator = request.locator_result

        if not locator.href:
            # Try to enrich using the matched text if available
            if request.txt:
                enriched = self.ebook_parser.find_text_location(
                    epub, request.txt, hint_percentage=pct
                )
                if enriched and enriched.href:
                    logger.debug(f"Enriched Storyteller locator with href={enriched.href}")
                    locator = enriched

            # Fallback: if we still don't have href, try to resolve from percentage
            if not locator.href:
                fallback_href = self._resolve_href_from_percentage(epub, pct)
                if fallback_href:
                    # Merge: keep the percentage but add the href
                    locator = LocatorResult(
                        percentage=pct,
                        href=fallback_href,
                        css_selector=None,
                        xpath=locator.xpath,
                        match_index=locator.match_index,
                        cfi=locator.cfi,
                        fragment=locator.fragment,
                        perfect_ko_xpath=locator.perfect_ko_xpath
                    )
                    logger.debug(f"Resolved Storyteller href from percentage: {locator.href}")

        if book.storyteller_uuid:
            success = self.storyteller_client.update_position(book.storyteller_uuid, pct, locator)
            if success:
                try:
                    from src.services.write_tracker import record_write
                    record_write('Storyteller', book.abs_id, pct)
                except ImportError:
                    pass
        else:
            # Strict mode: Do not update if not linked via UUID
            logger.debug(f"Skipping Storyteller update for {book.abs_title}: No linked UUID")
            success = False

        return SyncResult(pct, success)

    def _resolve_href_from_percentage(self, epub: str, pct: float) -> Optional[str]:
        """Find which spine item href contains the given percentage."""
        try:
            book_path = self.ebook_parser.resolve_book_path(epub)
            full_text, spine_map = self.ebook_parser.extract_text_and_map(book_path)
            if not full_text or not spine_map:
                return None
            target_index = int(len(full_text) * pct)
            for item in spine_map:
                if item['start'] <= target_index < item['end']:
                    return item['href']
        except Exception:
            pass
        return None




