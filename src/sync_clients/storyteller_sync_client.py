import os
from typing import Optional
import logging

from src.api.storyteller_api import StorytellerDBWithAPI
from src.db.models import Book, State
from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, LocatorResult, SyncResult, UpdateProgressRequest, ServiceState
logger = logging.getLogger(__name__)

class StorytellerSyncClient(SyncClient):
    def __init__(self, storyteller_client: StorytellerDBWithAPI, ebook_parser: EbookParser):
        super().__init__(ebook_parser)
        self.storyteller_client = storyteller_client
        self.ebook_parser = ebook_parser
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
        epub = book.ebook_filename
        st_pct, st_ts, st_href, st_frag = None, None, None, None

        # Try to use bulk context if available
        used_bulk = False
        if bulk_context:
            try:
                # Use the encapsulated find_book_by_title method
                book_info = self.storyteller_client.find_book_by_title(epub)
                if book_info:
                    title_key = book_info.get('title', '').lower()
                    if title_key in bulk_context:
                        data = bulk_context[title_key]
                        st_pct = data.get('pct')
                        st_ts = data.get('ts')
                        st_href = data.get('href')
                        st_frag = data.get('frag')
                        used_bulk = True
            except Exception as e:
                logger.debug(f"[{title_snip}] Failed to use Storyteller bulk context: {e}")

        if not used_bulk:
            st_pct, st_ts, st_href, st_frag = self.storyteller_client.get_progress_with_fragment(epub)

        no_position = False
        if st_pct is None:
            # Book may exist in Storyteller but have no reading position yet.
            # Treat as 0% so the sync cycle can push progress from other services.
            book_info = self.storyteller_client.find_book_by_title(epub)
            if book_info:
                st_pct = 0.0
                st_ts = 0
                no_position = True
                logger.info(f"[{title_snip}] Storyteller: book exists but no position, treating as 0%")
            else:
                logger.debug(f"[{title_snip}] Storyteller: book not found in library")
                return None

        # Get previous Storyteller state
        prev_storyteller_pct = prev_state.percentage if prev_state else 0

        # "No position" means absence of data, not a genuine move to 0%.
        # Force delta to 0 so Storyteller doesn't become leader by appearing
        # to have jumped backwards from a previously synced position.
        delta = 0 if no_position else abs(st_pct - prev_storyteller_pct)

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
        txt = self.ebook_parser.resolve_locator_id(epub, href, frag)
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
        else:
            success = self.storyteller_client.update_progress(epub, pct, locator)
        
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




