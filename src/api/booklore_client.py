# [START FILE: abs-kosync-enhanced/booklore_client.py]
import os
import time
import logging
from typing import Optional
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from pathlib import Path

from src.utils.logging_utils import sanitize_log_data
from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)

class BookloreClient:
    def __init__(self):
        self.base_url = os.environ.get("BOOKLORE_SERVER", "").rstrip('/')
        self.username = os.environ.get("BOOKLORE_USER")
        self.password = os.environ.get("BOOKLORE_PASSWORD")
        self._book_cache = {}
        self._book_id_cache = {}  # Maps book_id -> book_info for quick lookups
        self._cache_timestamp = 0
        self._token = None
        self._token_timestamp = 0
        self._token_max_age = 300
        self.session = requests.Session()

        # Cache file path
        self.cache_file = Path(os.environ.get("DATA_DIR", "/data")) / "booklore_cache.json"

        # Load cache on init
        self._load_cache()

    def _load_cache(self):
        """Load cache from disk."""
        if not self.cache_file.exists():
            return

        try:
            with open(self.cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self._book_cache = data.get('books', {})
                self._cache_timestamp = data.get('timestamp', 0)

                # Rebuild ID cache
                self._book_id_cache = {}
                for book in self._book_cache.values():
                    uid = book.get('id')
                    if uid:
                        self._book_id_cache[uid] = book

            logger.info(f"📚 Booklore: Loaded {len(self._book_cache)} books from disk cache")
        except Exception as e:
            logger.warning(f"Failed to load Booklore cache: {e}")
            self._book_cache = {}

    def _save_cache(self):
        """Save cache to disk."""
        try:
            data = {
                'timestamp': self._cache_timestamp,
                'books': self._book_cache
            }
            # Atomic write
            temp_file = self.cache_file.with_suffix('.tmp')
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            temp_file.replace(self.cache_file)
            logger.debug("Saved Booklore cache to disk")
        except Exception as e:
            logger.error(f"Failed to save Booklore cache: {e}")

    def _get_fresh_token(self):
        if self._token and (time.time() - self._token_timestamp) < self._token_max_age:
            return self._token
        if not all([self.base_url, self.username, self.password]): return None
        try:
            # Use session for login to handle cookies if needed
            response = self.session.post(
                f"{self.base_url}/api/v1/auth/login",
                json={"username": self.username, "password": self.password},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                # Booklore v1.17+ uses accessToken instead of token
                self._token = data.get("accessToken") or data.get("token")
                self._token_timestamp = time.time()
                return self._token
            else:
                logger.error(f"Booklore login failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"Booklore login error: {e}")
        return None

    def _make_request(self, method, endpoint, json_data=None):
        token = self._get_fresh_token()
        if not token: return None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{self.base_url}{endpoint}"
        try:
            if method.upper() == "GET":
                response = self.session.get(url, headers=headers, timeout=10)
            elif method.upper() == "POST":
                response = self.session.post(url, headers=headers, json=json_data, timeout=10)
            else: return None

            if response.status_code == 401:
                self._token = None
                token = self._get_fresh_token()
                if not token: return None
                headers["Authorization"] = f"Bearer {token}"
                if method.upper() == "GET":
                    response = self.session.get(url, headers=headers, timeout=10)
                else:
                    response = self.session.post(url, headers=headers, json=json_data, timeout=10)
            return response
        except Exception as e:
            logger.error(f"Booklore API request failed: {e}")
            return None

    def is_configured(self):
        """Return True if Booklore is configured, False otherwise."""
        enabled_val = os.environ.get("BOOKLORE_ENABLED", "").lower()
        if enabled_val == 'false':
            return False
        return bool(self.base_url and self.username and self.password)

    def check_connection(self):
        # Ensure Booklore is configured first
        if not all([self.base_url, self.username, self.password]):
            logger.info("⚠️ Booklore not configured (skipping)")
            return False

        token = self._get_fresh_token()
        if token:
            # If first run, show INFO; otherwise keep at DEBUG
            first_run_marker = '/data/.first_run_done'
            try:
                first_run = not os.path.exists(first_run_marker)
            except Exception:
                first_run = False

            if first_run:
                logger.info(f"✅ Connected to Booklore at {self.base_url}")
                try:
                    open(first_run_marker, 'w').close()
                except Exception:
                    pass
            return True

        # If we were configured but couldn't get a token, warn
        logger.warning("❌ Booklore connection failed: could not obtain auth token")
        return False

    def _fetch_book_detail(self, book_id, token):
        """Fetch individual book details to get fileName.

        Note: Uses requests directly instead of self.session for thread safety
        when called from ThreadPoolExecutor. Token is passed in to avoid
        concurrent token refresh issues.
        """
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"{self.base_url}/api/v1/books/{book_id}"
        try:
            # Use requests directly (not self.session) for thread safety
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception as e:
            logger.debug(f"Booklore: Error fetching book {book_id}: {e}")
            return None

    def _refresh_book_cache(self):
        """
        Refresh the book cache using robust pagination.
        Fetches books in batches to ensure complete library sync.
        """
        all_books_list = []
        page = 0
        batch_size = 200  # Reasonable chunk size
        
        logger.info("📚 Booklore: Starting full library scan...")
        
        while True:
            # Request specific page and size
            # Note: Booklore/Spring usually expects 'page' (0-indexed) and 'size'
            endpoint = f"/api/v1/books?page={page}&size={batch_size}"
            response = self._make_request("GET", endpoint)
            
            if not response or response.status_code != 200:
                logger.error(f"Booklore: Failed to fetch page {page}")
                return False

            data = response.json()
            
            # Handle different response shapes (List vs Page Object)
            current_batch = []
            if isinstance(data, list):
                current_batch = data
            elif isinstance(data, dict) and 'content' in data:
                # Spring Data Page object wrapper
                current_batch = data['content']
            
            if not current_batch:
                break  # No more books, we are done
                
            all_books_list.extend(current_batch)
            logger.debug(f"Booklore: Fetched page {page} ({len(current_batch)} items)")
            
            # If we got fewer items than requested, we are on the last page
            # Also break if we got MORE items than requested (server ignored size param)
            if len(current_batch) != batch_size:
                break
                
            page += 1

        if not all_books_list:
            logger.debug("Booklore: No books found in library")
            self._book_cache = {}
            self._book_id_cache = {}
            self._cache_timestamp = time.time()
            self._save_cache()
            return True

        logger.info(f"📚 Booklore: Scan complete. Found {len(all_books_list)} total books.")

        # --- Proceed with Step 2: Detail Fetching (Same as before) ---
        
        # Step 2: For books not in cache, fetch details to get fileName
        new_book_ids = [b['id'] for b in all_books_list if b['id'] not in self._book_id_cache]

        if new_book_ids:
            logger.debug(f"Booklore: Fetching details for {len(new_book_ids)} new books...")
            token = self._get_fresh_token()
            if not token: return False

            def fetch_one(book_id):
                return book_id, self._fetch_book_detail(book_id, token)

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {executor.submit(fetch_one, bid): bid for bid in new_book_ids}
                for future in as_completed(futures):
                    try:
                        book_id, detail = future.result()
                        if detail and isinstance(detail, dict):
                            self._process_book_detail(detail)
                    except Exception as e:
                        logger.debug(f"Booklore: Error fetching details: {e}")

        # Refresh existing items from the new list
        for book in all_books_list:
             if book['id'] in self._book_id_cache:
                 # Optional: Update shallow fields if needed
                 pass

        self._cache_timestamp = time.time()
        self._save_cache()
        return True

    def _process_book_detail(self, detail):
        """Process a book detail response and add to cache."""
        filename = detail.get('fileName', '')
        if not filename:
            return

        metadata = detail.get('metadata') or {}
        authors = metadata.get('authors') or []
        # Handle both list of strings and list of dicts for authors
        author_list = []
        for a in authors:
            if isinstance(a, dict):
                name = a.get('name', '')
                if name: author_list.append(name)
            elif isinstance(a, str) and a.strip():
                author_list.append(a.strip())

        author_str = ', '.join(author_list)
        subtitle = metadata.get('subtitle') or ''
        title = metadata.get('title') or detail.get('title') or filename

        book_info = {
            'id': detail.get('id'),
            'fileName': filename,
            'filePath': detail.get('filePath'),
            'title': title,
            'subtitle': subtitle,
            'authors': author_str,
            'bookType': detail.get('bookType'),
            'epubProgress': detail.get('epubProgress'),
            'pdfProgress': detail.get('pdfProgress'),
            'cbxProgress': detail.get('cbxProgress'),
            'koreaderProgress': detail.get('koreaderProgress'),
        }

        self._book_cache[filename.lower()] = book_info
        self._book_id_cache[detail['id']] = book_info

        return None

    def _normalize_string(self, s):
        """Remove non-alphanumeric characters and lowercase."""
        import re
        if not s: return ""
        return re.sub(r'[\W_]+', '', s.lower())

    def find_book_by_filename(self, ebook_filename, allow_refresh=True):
        """
        Find a book by its filename using exact, stem, or normalized matching.
        """
        # Ensure cache is initialized if empty, but respect allow_refresh for updates
        if not self._book_cache and allow_refresh: 
            self._refresh_book_cache()
            
        # Check cache freshness if refresh is allowed
        if allow_refresh and time.time() - self._cache_timestamp > 3600:
            self._refresh_book_cache()

        target_name = Path(ebook_filename).name.lower()
        
        # 1. Exact Filename Match
        if target_name in self._book_cache: return self._book_cache[target_name]

        target_stem = Path(ebook_filename).stem.lower()
        
        # 2. Strict Stem Match
        for cached_name, book_info in list(self._book_cache.items()):
            if Path(cached_name).stem.lower() == target_stem: return book_info

        # 3. Partial Stem Match
        for cached_name, book_info in list(self._book_cache.items()):
            if target_stem in cached_name or cached_name.replace('.epub', '') in target_stem:
                # High confidence check: ensure significant overlap
                return book_info

        # 4. Fuzzy / Normalized Match (Handling "Dragon's" vs "Dragons")
        # Use similarity ratio instead of substring to avoid false positives
        target_norm = self._normalize_string(target_stem)
        if len(target_norm) > 5:
            from difflib import SequenceMatcher
            best_match = None
            best_ratio = 0.0
            
            for cached_name, book_info in list(self._book_cache.items()):
                cached_norm = self._normalize_string(Path(cached_name).stem)
                # Calculate similarity ratio
                ratio = SequenceMatcher(None, target_norm, cached_norm).ratio()
                
                # Require high similarity (90%+) to avoid matching sequels
                if ratio > 0.90 and ratio > best_ratio:
                    best_ratio = ratio
                    best_match = (cached_name, book_info)
            
            if best_match:
                logger.debug(f"Fuzzy match: '{target_stem}' ~= '{best_match[0]}' (similarity: {best_ratio:.1%})")
                return best_match[1]

        # If not found, try refreshing cache once
        if allow_refresh and time.time() - self._cache_timestamp > 60:
            if self._refresh_book_cache():
                return self.find_book_by_filename(ebook_filename, allow_refresh=False)

        return None

    def get_all_books(self):
        """Get all books from cache, refreshing if necessary."""
        # Use a reasonable cache time of 1 hour, similar to find_book_by_filename
        if time.time() - self._cache_timestamp > 3600: self._refresh_book_cache()
        if not self._book_cache: self._refresh_book_cache()
        return list(self._book_cache.values())

    def search_books(self, search_term):
        """Search books by title, author, or filename. Returns list of matching books."""
        if time.time() - self._cache_timestamp > 5: self._refresh_book_cache()
        if not self._book_cache: self._refresh_book_cache()

        if not search_term:
            return list(self._book_cache.values())

        search_lower = search_term.lower()
        search_norm = self._normalize_string(search_term)
        
        results = []
        for book_info in list(self._book_cache.values()):
            title = (book_info.get('title') or '').lower()
            authors = (book_info.get('authors') or '').lower()
            filename = (book_info.get('fileName') or '').lower()

            # 1. Standard substring match
            if search_lower in title or search_lower in authors or search_lower in filename:
                results.append(book_info)
                continue
            
            # 2. Normalized match (for "Dragon's" vs "Dragons")
            # Only perform if standard match failed
            title_norm = self._normalize_string(title)
            authors_norm = self._normalize_string(authors)
            filename_norm = self._normalize_string(filename)
            
            if len(search_norm) > 3: # Avoid extremely short noisy matches
                if (search_norm in title_norm or 
                    search_norm in authors_norm or 
                    search_norm in filename_norm):
                    results.append(book_info)
        
        return results

    def download_book(self, book_id):
        """Download book content by ID. Returns bytes or None."""
        token = self._get_fresh_token()
        if not token: return None

        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.base_url}/api/v1/books/{book_id}/download"

        try:
            response = self.session.get(url, headers=headers, timeout=60)
            if response.status_code == 200:
                return response.content
            else:
                logger.error(f"Booklore download failed: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Booklore download error: {e}")
            return None

    def get_progress(self, ebook_filename):
        book = self.find_book_by_filename(ebook_filename)
        if not book: return None, None

        response = self._make_request("GET", f"/api/v1/books/{book['id']}")
        if response and response.status_code == 200:
            data = response.json()
            book_type = data.get('bookType', '').upper()
            if book_type == 'EPUB':
                progress = data.get('epubProgress') or {}
                pct = progress.get('percentage', 0)
                return (pct / 100.0 if pct else 0.0), progress.get('cfi')
            elif book_type == 'PDF':
                progress = data.get('pdfProgress') or {}
                pct = progress.get('percentage', 0)
                return (pct / 100.0 if pct else 0.0), None
            elif book_type == 'CBX':
                progress = data.get('cbxProgress') or {}
                pct = progress.get('percentage', 0)
                return (pct / 100.0 if pct else 0.0), None
        return None, None

    def update_progress(self, ebook_filename, percentage, rich_locator: Optional[LocatorResult] = None):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            logger.debug(f"Booklore: Book not found: {ebook_filename}")
            return False

        book_id = book['id']
        book_type = (book.get('bookType') or '').upper()
        pct_display = percentage * 100
        cfi = rich_locator.cfi if rich_locator and rich_locator.cfi else None

        if book_type == 'EPUB':
            payload = {"bookId": book_id, "epubProgress": {"percentage": pct_display}}
            if cfi:
                payload["epubProgress"]["cfi"] = cfi
                logger.debug(f"Booklore: Setting CFI: {cfi}")
        elif book_type == 'PDF':
            payload = {"bookId": book_id, "pdfProgress": {"page": 1, "percentage": pct_display}}
        elif book_type == 'CBX':
            payload = {"bookId": book_id, "cbxProgress": {"page": 1, "percentage": pct_display}}
        else:
            logger.warning(f"Booklore: Unknown book type {book_type} for {sanitize_log_data(ebook_filename)}")
            return False

        response = self._make_request("POST", "/api/v1/books/progress", payload)
        if response and response.status_code in [200, 201, 204]:
            logger.info(f"✅ Booklore: {sanitize_log_data(ebook_filename)} → {pct_display:.1f}%")
            # Refresh cache to reflect recent update so subsequent gets return fresh values
            try:
                self._refresh_book_cache()
            except Exception:
                logger.debug("Booklore: Cache refresh failed after update")
            return True
        else:
            status = response.status_code if response else "No response"
            logger.error(f"Booklore update failed: {status}")
            return False

    def get_recent_activity(self, min_progress=0.01):
        if not self._book_cache: self._refresh_book_cache()
        results = []
        for filename, book in list(self._book_cache.items()):
            progress = 0
            if book.get('epubProgress'):
                progress = (book['epubProgress'].get('percentage') or 0) / 100.0
            elif book.get('pdfProgress'):
                progress = (book['pdfProgress'].get('percentage') or 0) / 100.0
            elif book.get('cbxProgress'):
                progress = (book['cbxProgress'].get('percentage') or 0) / 100.0
            if progress >= min_progress:
                results.append({
                    "id": book['id'],
                    "filename": book['fileName'],
                    "progress": progress,
                    "source": "BOOKLORE"
                })
        return results

    def add_to_shelf(self, ebook_filename, shelf_name=None):
        """Add a book to a shelf, creating the shelf if it doesn't exist."""
        if not shelf_name:
             shelf_name = os.environ.get("BOOKLORE_SHELF_NAME", "abs-kosync")

        try:
            # Find the book
            book = self.find_book_by_filename(ebook_filename)
            if not book:
                logger.warning(f"Booklore: Book not found for shelf assignment: {sanitize_log_data(ebook_filename)}")
                return False

            # Get or create shelf
            shelves_response = self._make_request("GET", "/api/v1/shelves")
            if not shelves_response or shelves_response.status_code != 200:
                logger.error("Failed to get Booklore shelves")
                return False

            shelves = shelves_response.json()
            target_shelf = next((s for s in shelves if s.get('name') == shelf_name), None)

            if not target_shelf:
                # Create shelf
                create_response = self._make_request("POST", "/api/v1/shelves", {
                    "name": shelf_name,
                    "icon": "📚",
                    "iconType": "PRIME_NG"
                })
                if not create_response or create_response.status_code != 201:
                    logger.error(f"Failed to create Booklore shelf: {shelf_name}")
                    return False
                target_shelf = create_response.json()

            # Assign book to shelf
            assign_response = self._make_request("POST", "/api/v1/books/shelves", {
                "bookIds": [book['id']],
                "shelvesToAssign": [target_shelf['id']],
                "shelvesToUnassign": []
            })

            if assign_response and assign_response.status_code in [200, 201, 204]:
                logger.info(f"🏷️ Added '{sanitize_log_data(ebook_filename)}' to Booklore Shelf: {shelf_name}")
                return True
            else:
                logger.error(f"Failed to assign book to shelf. Status: {assign_response.status_code if assign_response else 'No response'}")
                return False

        except Exception as e:
            logger.error(f"Error adding book to Booklore shelf: {e}")
            return False

    def remove_from_shelf(self, ebook_filename, shelf_name=None):
        """Remove a book from a shelf."""
        if not shelf_name:
             shelf_name = os.environ.get("BOOKLORE_SHELF_NAME", "abs-kosync")

        try:
            # Find the book
            book = self.find_book_by_filename(ebook_filename)
            if not book:
                logger.warning(f"Booklore: Book not found for shelf removal: {sanitize_log_data(ebook_filename)}")
                return False

            # Get shelf
            shelves_response = self._make_request("GET", "/api/v1/shelves")
            if not shelves_response or shelves_response.status_code != 200:
                logger.error("Failed to get Booklore shelves")
                return False

            shelves = shelves_response.json()
            target_shelf = next((s for s in shelves if s.get('name') == shelf_name), None)

            if not target_shelf:
                logger.warning(f"Shelf '{shelf_name}' not found")
                return False

            # Remove from shelf
            assign_response = self._make_request("POST", "/api/v1/books/shelves", {
                "bookIds": [book['id']],
                "shelvesToAssign": [],
                "shelvesToUnassign": [target_shelf['id']]
            })

            if assign_response and assign_response.status_code in [200, 201, 204]:
                logger.info(f"🗑️ Removed '{sanitize_log_data(ebook_filename)}' from Booklore Shelf: {shelf_name}")
                return True
            else:
                logger.error(f"Failed to remove book from shelf. Status: {assign_response.status_code if assign_response else 'No response'}")
                return False

        except Exception as e:
            logger.error(f"Error removing book from Booklore shelf: {e}")
            return False
# [END FILE]