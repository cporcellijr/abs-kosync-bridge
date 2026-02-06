"""
Library Service.
Handles high-level book management, bridging the gap between
AudioBookShelf (ABS), Booklore (Metadata), and our local database.
"""

import logging
from typing import List

from src.db.models import Book
from src.db.database_service import DatabaseService

logger = logging.getLogger(__name__)

class LibraryService:
    def __init__(self, database_service: DatabaseService, booklore_client):
        self.db = database_service
        self.booklore = booklore_client

    def get_syncable_books(self) -> List[Book]:
        """
        Returns a list of books that are active and candidates for synchronization.
        """
        # This wraps the low-level DB query
        return self.db.get_all_books()

    def sync_library_books(self):
        """
        Main Routine: Synchronize our local library DB with external metadata sources (Booklore).
        
        The new BookloreClient handles its own file-based caching internally.
        This method now simply triggers a cache refresh by calling get_all_books().
        """
        books = self.get_syncable_books()
        logger.info(f"📚 LibraryService: Syncing metadata for {len(books)} books...")
        
        # Check if Booklore is configured
        if not self.booklore or not self.booklore.is_configured():
            logger.info("   Booklore not configured, skipping library sync.")
            return
            
        logger.info("✅ Booklore integration enabled - ebooks sourced from API")
        
        # Trigger cache refresh by calling get_all_books()
        # This will refresh the internal JSON-based cache if stale
        try:
            from src.db.models import BookloreBook
            import json

            all_books = self.booklore.get_all_books()
            logger.info(f"   📚 Booklore API returned {len(all_books)} books. Persisting to DB...")
            
            persisted_count = 0
            for b in all_books:
                booklore_book = BookloreBook(
                    filename=b.get('fileName'),
                    title=b.get('title'),
                    authors=b.get('authors'),
                    raw_metadata=json.dumps(b)
                )
                self.db.save_booklore_book(booklore_book)
                persisted_count += 1
            
            logger.info(f"   ✅ Successfully persisted {persisted_count} books to BookloreBook table")
        except Exception as e:
            logger.error(f"   Library sync failed: {e}")

