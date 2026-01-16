import logging
from typing import Optional

from src.api.hardcover_client import HardcoverClient
from src.db.models import Book, State, HardcoverDetails
from src.sync_clients.sync_client_interface import SyncClient, SyncResult, UpdateProgressRequest, ServiceState
from src.utils.ebook_utils import EbookParser
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)


class HardcoverSyncClient(SyncClient):
    """
    Hardcover sync client that handles both automating matching and progress sync.
    This integrates Hardcover as a proper sync client in the sync cycle.
    """

    def __init__(self, hardcover_client: HardcoverClient, ebook_parser: EbookParser, abs_client=None, database_service=None):
        super().__init__(ebook_parser)
        self.hardcover_client = hardcover_client
        self.abs_client = abs_client  # For fetching book metadata
        self.database_service = database_service

    def is_configured(self) -> bool:
        """Check if Hardcover is configured."""
        return self.hardcover_client.is_configured()

    def check_connection(self):
        """Check connection to Hardcover API."""
        return self.hardcover_client.check_connection()

    def can_be_leader(self) -> bool:
        """
        Hardcover cannot be a leader because it doesn't provide text content
        for synchronization. It only receives updates from other clients.
        """
        return False

    def get_service_state(self, book: Book, prev_state: Optional[State], title_snip: str = "") -> Optional[ServiceState]:
        """
        Since Hardcover can never be the leader, its service state is not used for
        leader selection or text extraction. Return None to indicate no state needed.
        Auto-matching and progress sync happen in update_progress when actually needed.
        """
        return None


    def _automatch_hardcover(self, book):
        """
        Match a book with Hardcover using various search strategies.
        Moved from sync_manager.py to make hardcover a proper sync client.
        """
        if not self.hardcover_client.is_configured() or not self.abs_client or not self.database_service:
            return

        # Check if we already have hardcover details for this book
        existing_details = self.database_service.get_hardcover_details(book.abs_id)
        if existing_details:
            return  # Already matched

        item = self.abs_client.get_item_details(book.abs_id)
        if not item:
            return

        meta = item.get('media', {}).get('metadata', {})
        match = None
        matched_by = None

        # Extract metadata fields for clarity
        isbn = meta.get('isbn')
        asin = meta.get('asin')
        title = meta.get('title')
        author = meta.get('authorName')

        # Try different search strategies in order of preference
        if isbn:
            match = self.hardcover_client.search_by_isbn(isbn)
            if match:
                matched_by = 'isbn'

        if not match and asin:
            match = self.hardcover_client.search_by_isbn(asin)
            if match:
                matched_by = 'asin'

        if not match and title and author:
            match = self.hardcover_client.search_by_title_author(title, author)
            if match:
                matched_by = 'title_author'

        if not match and title:
            match = self.hardcover_client.search_by_title_author(title, "")
            if match:
                matched_by = 'title'

        if match:
            # Create HardcoverDetails model
            hardcover_details = HardcoverDetails(
                abs_id=book.abs_id,
                hardcover_book_id=match.get('book_id'),
                hardcover_edition_id=match.get('edition_id'),
                hardcover_pages=match.get('pages'),
                isbn=isbn,
                asin=asin,
                matched_by=matched_by
            )

            # Save to database
            self.database_service.save_hardcover_details(hardcover_details)

            self.hardcover_client.update_status(int(match.get('book_id')), 1, match.get('edition_id'))
            logger.info(f"📚 Hardcover: '{sanitize_log_data(meta.get('title'))}' matched and set to Want to Read (matched by {matched_by})")

    def set_manual_match(self, book_abs_id: str, input_str: str) -> bool:
        """
        Manually match an ABS book to a Hardcover book via URL, ID, or Slug.
        """
        if not self.hardcover_client.is_configured():
            logger.error("Hardcover client not configured")
            return False

        # Resolve the input string to a Hardcover book
        match = self.hardcover_client.resolve_book_from_input(input_str)
        if not match:
            logger.error(f"Could not resolve Hardcover book from '{input_str}'")
            return False

        # Try to get existing metadata from ABS for completeness
        isbn = None
        asin = None
        
        if self.abs_client:
            try:
                item = self.abs_client.get_item_details(book_abs_id)
                if item:
                    meta = item.get('media', {}).get('metadata', {})
                    isbn = meta.get('isbn')
                    asin = meta.get('asin')
            except Exception as e:
                logger.warning(f"Failed to fetch ABS details during manual match: {e}")

        # Create/Update HardcoverDetails
        # We need to upsert. save_hardcover_details likely handles upsert (merging/replacing).
        details = HardcoverDetails(
            abs_id=book_abs_id,
            hardcover_book_id=match['book_id'],
            hardcover_edition_id=match.get('edition_id'),
            hardcover_pages=match.get('pages'),
            isbn=isbn,
            asin=asin,
            matched_by='manual'
        )
        
        self.database_service.save_hardcover_details(details)
        logger.info(f"✅ Manually matched ABS {book_abs_id} to Hardcover {match['book_id']} ({match.get('title')})")
        
        # Trigger an initial status update to ensure it's tracked
        self.hardcover_client.update_status(match['book_id'], 1, match.get('edition_id'))
        return True

    def get_text_from_current_state(self, book: Book, state: ServiceState) -> Optional[str]:
        """
        Hardcover doesn't provide text content, so return None.
        This client is primarily for progress synchronization.
        """
        return None

    def update_progress(self, book: Book, request: UpdateProgressRequest) -> SyncResult:
        """
        Update progress in Hardcover based on the incoming locator result.
        Performs auto-matching if needed before syncing progress.
        """
        if not self.is_configured() or not self.database_service:
            return SyncResult(None, False)

        # Ensure we have hardcover details (auto-match if needed)
        self._automatch_hardcover(book)

        percentage = request.locator_result.percentage

        # Get hardcover details for this book
        hardcover_details = self.database_service.get_hardcover_details(book.abs_id)
        if not hardcover_details or not hardcover_details.hardcover_book_id:
            # No match found and auto-matching failed
            return SyncResult(None, False)

        # Get user book from Hardcover
        ub = self.hardcover_client.get_user_book(hardcover_details.hardcover_book_id)
        if not ub:
            return SyncResult(None, False)

        total_pages = hardcover_details.hardcover_pages or 0

        # Safety check: If total_pages is zero we cannot compute a valid page number
        if total_pages == 0:
            # Attempt to refresh page count if it is zero (maybe the edition was updated or we matched a bad edition)
            logger.info(f"Hardcover: Pages are 0 for {sanitize_log_data(book.abs_title)}, attempting to refresh details...")
            refreshed_edition = self.hardcover_client.get_default_edition(hardcover_details.hardcover_book_id)
            if refreshed_edition and refreshed_edition.get('pages'):
                total_pages = refreshed_edition['pages']
                # Update DB
                hardcover_details.hardcover_pages = total_pages
                hardcover_details.hardcover_edition_id = refreshed_edition['id']
                self.database_service.save_hardcover_details(hardcover_details)
                logger.info(f"Hardcover: Updated page count to {total_pages}")
            else:
                logger.info(f"⚠️ Hardcover Sync Skipped: {sanitize_log_data(book.abs_title)} still has 0 pages after refresh.")
                return SyncResult(None, False)

        page_num = int(total_pages * percentage)
        is_finished = percentage > 0.99
        current_status = ub.get('status_id')

        # Handle Status Changes
        # If Finished, prefer marking as Read (3) first
        if is_finished and current_status != 3:
            self.hardcover_client.update_status(
                hardcover_details.hardcover_book_id,
                3,
                hardcover_details.hardcover_edition_id
            )
            logger.info(f"📚 Hardcover: '{sanitize_log_data(book.abs_title)}' status promoted to Read")
            current_status = 3

        # If progress > 2% and currently "Want to Read" (1), switch to "Currently Reading" (2)
        elif percentage > 0.02 and current_status == 1:
            self.hardcover_client.update_status(
                hardcover_details.hardcover_book_id,
                2,
                hardcover_details.hardcover_edition_id
            )
            logger.info(f"📚 Hardcover: '{sanitize_log_data(book.abs_title)}' status promoted to Currently Reading")
            current_status = 2

        # Update progress (Hardcover rejects page updates for Want to Read)
        try:
            self.hardcover_client.update_progress(
                ub['id'],
                page_num,
                edition_id=hardcover_details.hardcover_edition_id,
                is_finished=is_finished,
                current_percentage=percentage
            )

            # Calculate the actual percentage from the page number for state tracking
            actual_pct = min(page_num / total_pages, 1.0) if total_pages > 0 else percentage

            updated_state = {
                'pct': actual_pct,
                'pages': page_num,
                'total_pages': total_pages,
                'status': current_status
            }

            return SyncResult(actual_pct, True, updated_state)

        except Exception as e:
            logger.error(f"Failed to update Hardcover progress: {e}")
            return SyncResult(None, False)
