"""
StoryGraph Sync Client - integrates StoryGraph as a sync target.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from src.api.storygraph_client import StoryGraphClient
from src.db.models import Book, State, StoryGraphDetails
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

# Retry 'not_found' entries after this duration
NOT_FOUND_RETRY_HOURS = 24

STORYGRAPH_DELTA_THRESHOLD = 0.01  # 1%


class StoryGraphSyncClient(SyncClient):
    """StoryGraph sync client for progress tracking."""
    
    def __init__(
        self,
        storygraph_client: StoryGraphClient,
        ebook_parser: EbookParser,
        abs_client=None,
        database_service=None
    ):
        super().__init__(ebook_parser)
        self.storygraph_client = storygraph_client
        self.abs_client = abs_client
        self.database_service = database_service
        self._last_synced_progress = {}
    
    def is_configured(self) -> bool:
        # Check environment/settings and client config
        # We re-check these here because settings might change at runtime
        return self.storygraph_client is not None and self.storygraph_client.is_configured()
    
    def check_connection(self) -> bool:
        if not self.is_configured():
            raise Exception("StoryGraph not configured")
        return True  # Passive check
    
    def can_be_leader(self) -> bool:
        return False  # StoryGraph is write-only
    
    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "", bulk_context: dict = None) -> Optional[ServiceState]:
        # StoryGraph is write-only, we don't fetch state from it
        return None
    
    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        return None
    
    def _automatch_storygraph(self, book: Book) -> Optional[StoryGraphDetails]:
        if not self.is_configured() or not self.database_service:
            return None
        
        # Check DB first
        existing = self.database_service.get_storygraph_details(book.abs_id)
        if existing:
            # If matched_by is 'not_found', retry after NOT_FOUND_RETRY_HOURS
            if existing.matched_by == 'not_found':
                if existing.updated_at:
                    retry_after = existing.updated_at + timedelta(hours=NOT_FOUND_RETRY_HOURS)
                    if datetime.utcnow() < retry_after:
                        logger.debug(f"📚 StoryGraph: '{sanitize_log_data(book.abs_title)}' marked not_found, will retry after {retry_after}")
                        return None
                    else:
                        logger.info(f"🔄 StoryGraph: Retrying search for '{sanitize_log_data(book.abs_title)}' (not_found expired)")
                        # Delete old entry so we can try again
                        self.database_service.delete_storygraph_details(book.abs_id)
                else:
                    # No timestamp, skip
                    logger.debug(f"📚 StoryGraph: '{sanitize_log_data(book.abs_title)}' marked not_found (no retry timestamp)")
                    return None
            else:
                logger.debug(f"📚 StoryGraph: Using cached match for '{sanitize_log_data(book.abs_title)}'")
                return existing
        
        # Need ABS client to get metadata
        if not self.abs_client:
            return None
        
        item = self.abs_client.get_item_details(book.abs_id)
        if not item:
            return None
        
        meta = item.get('media', {}).get('metadata', {})
        title = meta.get('title')
        author = meta.get('authorName')
        
        if not title:
            return None
        
        # New: Try ISBN from Hardcover if available
        if book.hardcover_details and book.hardcover_details.isbn:
            isbn = book.hardcover_details.isbn
            logger.info(f"🔎 StoryGraph: Trying ISBN match for '{sanitize_log_data(title)}' ({isbn})")
            match = self.storygraph_client.search_book(isbn)
            if match:
                details = StoryGraphDetails(
                    abs_id=book.abs_id,
                    storygraph_id=match['book_id'],
                    storygraph_title=match.get('title'),
                    storygraph_author=match.get('author'),
                    storygraph_pages=match.get('pages', 0),
                    storygraph_url=match.get('url'),
                    matched_by='isbn'
                )
                self.database_service.save_storygraph_details(details)
                logger.info(f"✅ StoryGraph: Matched by ISBN to '{sanitize_log_data(match.get('title'))}'")
                return details

        logger.info(f"🔎 StoryGraph: Searching for '{sanitize_log_data(title)}'...")
        match = self.storygraph_client.search_book(title, author)
        
        if not match:
            # Save negative result so we don't spam search
            details = StoryGraphDetails(abs_id=book.abs_id, matched_by='not_found')
            self.database_service.save_storygraph_details(details)
            return None
        
        details = StoryGraphDetails(
            abs_id=book.abs_id,
            storygraph_id=match['book_id'],
            storygraph_title=match.get('title'),
            storygraph_author=match.get('author'),
            storygraph_pages=match.get('pages', 0),
            storygraph_url=match.get('url'),
            matched_by='title_author'
        )
        self.database_service.save_storygraph_details(details)
        
        logger.info(f"✅ StoryGraph: Matched to '{sanitize_log_data(match.get('title'))}'")
        return details
    
    def _should_sync(self, book: Book, new_percentage: float) -> bool:
        last_pct = self._last_synced_progress.get(book.abs_id, 0)
        # Sync if delta > 1% OR if we haven't synced this session (last_pct is 0 but new is > 0)
        # But wait, 0 could be valid. Let's stick to simple delta.
        return abs(new_percentage - last_pct) >= STORYGRAPH_DELTA_THRESHOLD
    
    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        if not self.is_configured() or not self.database_service:
             return SyncResult(None, False)
        
        percentage = request.locator_result.percentage
        
        if not self._should_sync(book, percentage):
            return SyncResult(percentage, True, {'skipped': True})
        
        try:
            details = self._automatch_storygraph(book)
            
            if not details or not details.storygraph_id:
                # No match found
                return SyncResult(None, False)
            
            total_pages = details.storygraph_pages or 0
            
            # If we don't have page count, we can't really update pages accurately
            # StoryGraph supports percentage updates? The prompt used pages.
            # Let's assume pages for now.
            current_page = int(total_pages * percentage) if total_pages > 0 else 0
            is_finished = percentage > 0.99
            
            success = self.storygraph_client.update_progress(
                book_id=details.storygraph_id,
                pages_read=current_page,
                total_pages=total_pages,
                progress_percent=percentage,
                is_finished=is_finished
            )
            
            if success:
                self._last_synced_progress[book.abs_id] = percentage
                updated_data = {'pages': current_page, 'total_pages': total_pages}
                logger.info(f"✅ StoryGraph Sync: {updated_data}")
                return SyncResult(percentage, True, updated_data)
        
        except Exception as e:
            logger.error(f"Failed to sync to StoryGraph: {e}")
            try:
                logger.error(f"DEBUG: database_service attributes: {dir(self.database_service)}")
            except:
                pass
            
        return SyncResult(None, False)
