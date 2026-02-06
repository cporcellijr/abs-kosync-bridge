"""
Library Service.
Handles high-level book management, bridging the gap between
AudioBookShelf (ABS), Booklore (Metadata), and our local database.
"""

import logging
import os
import glob
import shutil
from pathlib import Path
from typing import List, Optional

from src.db.models import Book
from src.db.database_service import DatabaseService
from src.api.api_clients import ABSClient
from src.api.cwa_client import CWAClient

logger = logging.getLogger(__name__)

class LibraryService:
    def __init__(self, database_service: DatabaseService, booklore_client, cwa_client: CWAClient, abs_client: ABSClient, epub_cache_dir: str):
        self.db = database_service
        self.booklore = booklore_client
        self.cwa_client = cwa_client
        self.abs_client = abs_client
        self.epub_cache_dir = epub_cache_dir
        
        if not os.path.exists(self.epub_cache_dir):
            try:
                os.makedirs(self.epub_cache_dir)
            except Exception:
                pass

        # Log configuration status for visibility
        if self.cwa_client and self.cwa_client.is_configured():
            logger.info("[INIT] CWA Integration enabled")
        else:
            logger.info("[SKIP] CWA Integration disabled/unconfigured")

        if self.booklore and (hasattr(self.booklore, 'is_configured') and self.booklore.is_configured()):
            logger.info("[INIT] Booklore Integration enabled")
        else:
            logger.info("[SKIP] Booklore Integration disabled/unconfigured")

    def get_syncable_books(self) -> List[Book]:
        """
        Returns a list of books that are active and candidates for synchronization.
        """
        # This wraps the low-level DB query
        return self.db.get_all_books()

    def acquire_ebook(self, abs_item: dict) -> Optional[str]:
        """
        Attempt to acquire an ebook for the given audiobook item.
        Priority Chain:
        1. ABS Direct Match (Audiobook item has ebook file)
        2. Booklore (Curated DB Match)
        3. CWA (Automated Library Search via OPDS)
        4. ABS Search (Search other libraries for title)
        5. Filesystem (Fallback - handled by caller)
        
        Returns:
            Absolute path to the downloaded/found ebook, or None.
        """
        item_id = abs_item.get('id')
        title = abs_item.get('media', {}).get('metadata', {}).get('title')
        author = abs_item.get('media', {}).get('metadata', {}).get('authorName')
        
        # Sanity check
        if not item_id or not title:
            return None

        logger.info(f"📚 Acquiring ebook for: {title} ({item_id})")

        # 1. ABS Direct Match
        if self.abs_client:
            ebooks = self.abs_client.get_ebook_files(item_id)
            if ebooks:
                logger.info(f"   ✅ Priority 1 (ABS Direct): Found {len(ebooks)} ebook(s) in item.")
                target = ebooks[0]
                filename = f"{item_id}_direct.{target['ext']}"
                output_path = os.path.join(self.epub_cache_dir, filename)
                
                # Check if already exists?
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    logger.info(f"   Using cached ebook: {output_path}")
                    return output_path

                if self.abs_client.download_file(target['stream_url'], output_path):
                     logger.info(f"   ⬇️ Downloaded direct match to {output_path}")
                     return output_path

        # 2. Booklore (Curated)
        # Placeholder for curated DB lookup. 
        # Future: Check self.db.find_booklore_match(title, author)
        
        # 3. CWA (OPDS)
        if self.cwa_client and self.cwa_client.is_configured():
            # Use title + author for better precision
            query = f"{title}"
            if author:
                query += f" {author}"
            
            results = self.cwa_client.search_ebooks(query)
            if results:
                logger.info(f"   ✅ Priority 3 (CWA): Found {len(results)} matches for '{query}'")
                target = results[0]
                filename = f"{item_id}_cwa.{target['ext']}"
                output_path = os.path.join(self.epub_cache_dir, filename)
                
                if self.cwa_client.download_ebook(target['download_url'], output_path):
                    logger.info(f"   ⬇️ Downloaded CWA match to {output_path}")
                    return output_path
            else:
                 logger.debug(f"   CWA: No matches for '{query}'")

        # 4. ABS Library Scan (Fallback for Mixed Content)
        # If the item exists in the SAME library but as a separate "book" item (and search fails),
        # we scan the library directly.
        if self.abs_client and abs_item.get('libraryId'):
            lib_id = abs_item['libraryId']
            logger.info(f"   🔍 Priority 4 (Library Scan): Scanning library {lib_id} for '{title}'...")
            
            matches = self.abs_client.find_book_in_library(lib_id, title, author)
            for match in matches:
                # Avoid re-checking the same item if checking direct match failed already
                if match['id'] == item_id: continue
                
                # Check for ebook files in this match
                target_files = self.abs_client.get_ebook_files(match['id'])
                if target_files:
                    tf = target_files[0]
                    filename = f"{item_id}_libscan.{tf['ext']}"
                    output_path = os.path.join(self.epub_cache_dir, filename)
                    
                    if self.abs_client.download_file(tf['stream_url'], output_path):
                        logger.info(f"   ⬇️ Downloaded Library Scan match to {output_path}")
                        return output_path
            
            if not matches:
                logger.debug("   Library Scan: No matches found.")

        if self.abs_client:
             results = self.abs_client.search_ebooks(title)
             if results:
                 logger.info(f"   ✅ Priority 5 (ABS Search): Found {len(results)} matches for '{title}'")
                 # Try to find one with ebook files
                 for res in results:
                     # Check if author matches loosely
                     res_author = res.get('author', '')
                     if author and author.lower() not in res_author.lower() and res_author.lower() not in author.lower():
                         continue

                     target_files = self.abs_client.get_ebook_files(res['id'])
                     if target_files:
                         tf = target_files[0]
                         filename = f"{item_id}_abs_search.{tf['ext']}"
                         output_path = os.path.join(self.epub_cache_dir, filename)
                         
                         if self.abs_client.download_file(tf['stream_url'], output_path):
                             logger.info(f"   ⬇️ Downloaded ABS search match to {output_path}")
                             return output_path
                         break

        return None

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

