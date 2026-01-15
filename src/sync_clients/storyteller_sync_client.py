import os
from typing import Optional
import logging

from src.utils.ebook_utils import EbookParser
from src.sync_clients.sync_client_interface import SyncClient, LocatorResult, SyncResult, UpdateProgressRequest, ServiceState
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
                fallback_locator = self._resolve_href_from_percentage(epub, pct)
                if fallback_locator and fallback_locator.href:
                    # Merge: keep the percentage but add the href
                    locator = LocatorResult(
                        percentage=pct,
                        href=fallback_locator.href,
                        css_selector=fallback_locator.css_selector,
                        xpath=locator.xpath,
                        match_index=locator.match_index,
                        cfi=locator.cfi,
                        fragment=locator.fragment,
                        perfect_ko_xpath=locator.perfect_ko_xpath
                    )
                    logger.debug(f"Resolved Storyteller href from percentage: {locator.href}")

        success = self.storyteller_db.update_progress(epub, pct, locator)
        return SyncResult(pct, success)

    def _resolve_href_from_percentage(self, epub: str, pct: float) -> Optional[str]:
        """Find which spine item href contains the given percentage."""
        try:
            book_path = self.ebook_parser._resolve_book_path(epub)
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
         
    

