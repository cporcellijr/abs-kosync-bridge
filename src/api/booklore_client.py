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
    def __init__(self, database_service=None):
        raw_url = os.environ.get("BOOKLORE_SERVER", "").rstrip('/')
        if raw_url and not raw_url.lower().startswith(('http://', 'https://')):
            raw_url = f"http://{raw_url}"
        self.base_url = raw_url
        self.username = os.environ.get("BOOKLORE_USER")
        self.password = os.environ.get("BOOKLORE_PASSWORD")
        self.db = database_service
        
        # In-memory cache for performance (populated from DB)
        self._book_cache = {} 
        self._book_id_cache = {}
        self._cache_timestamp = 0
        
        self._token = None
        self._token_timestamp = 0
        self._token_max_age = 300
        self.session = requests.Session()

        # Legacy Cache file path (for migration only)
        self.legacy_cache_file = Path(os.environ.get("DATA_DIR", "/data")) / "booklore_cache.json"

        # Load cache from DB (and migrate if needed)
        self.target_library_id = os.environ.get("BOOKLORE_LIBRARY_ID")
        self._load_cache()

    def _load_cache(self):
        """Load cache from DB, migrating legacy JSON if needed."""
        # 1. Migrate Legacy JSON if it exists and DB is empty
        if self.legacy_cache_file.exists():
            try:
                # Check if DB is empty to avoid overwriting newer SQL data
                if self.db and not self.db.get_all_booklore_books():
                    logger.info("📦 Booklore: Migrating legacy JSON cache to SQLite...")
                    with open(self.legacy_cache_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        books = data.get('books', {})
                        count = 0
                        for filename, book_info in books.items():
                            try:
                                from src.db.models import BookloreBook
                                import json as pyjson
                                
                                # Convert book_info to BookloreBook model
                                b_model = BookloreBook(
                                    filename=filename,
                                    title=book_info.get('title'),
                                    authors=book_info.get('authors'),
                                    raw_metadata=pyjson.dumps(book_info)
                                )
                                self.db.save_booklore_book(b_model)
                                count += 1
                            except Exception as e:
                                logger.warning(f"⚠️ Failed to migrate book {filename}: {e}")
                        
                        logger.info(f"✅ Booklore: Migrated {count} books to database.")
                        
                    # Rename legacy file to .bak after successful migration
                    try:
                        self.legacy_cache_file.rename(self.legacy_cache_file.with_suffix('.json.bak'))
                        logger.info("📦 Booklore: Legacy cache file renamed to .bak")
                    except Exception as e:
                        logger.warning(f"⚠️ Could not rename legacy cache file: {e}")
            except Exception as e:
                logger.error(f"❌ Booklore migration failed: {e}")

        # 2. Load from DB into memory
        if self.db:
            try:
                db_books = self.db.get_all_booklore_books()
                self._book_cache = {}
                self._book_id_cache = {}
                
                for db_book in db_books:
                    # Parse raw metadata back to dict
                    book_info = db_book.raw_metadata_dict
                    # Ensure minimal fields exist
                    if not book_info:
                        book_info = {
                            'fileName': db_book.filename,
                            'title': db_book.title,
                            'authors': db_book.authors
                        }
                    
                    self._book_cache[db_book.filename] = book_info
                    
                    # Update ID cache
                    bid = book_info.get('id')
                    if bid:
                        self._book_id_cache[bid] = book_info
                        
                # Set to 0 to force a refresh/validation against API on next access
                self._cache_timestamp = 0
                logger.info(f"📚 Booklore: Loaded {len(self._book_cache)} books from database")
            except Exception as e:
                logger.error(f"❌ Failed to load Booklore cache from DB: {e}")
                self._book_cache = {}

    def _save_cache(self):
        """
        Save cache to DB.
        Note: We now save individual books on update, so this is mostly a no-op 
        or used for bulk updates/timestamp management.
        """
        pass # Database persistence is handled atomically per book elsewhere

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
                logger.error(f"❌ Booklore login failed: {response.status_code} - {response.text}")
        except Exception as e:
            logger.error(f"❌ Booklore login error: {e}")
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
            logger.error(f"❌ Booklore API request failed: {e}")
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
            logger.warning("⚠️ Booklore not configured (skipping)")
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
        logger.error("❌ Booklore connection failed: could not obtain auth token")
        return False

    def get_libraries(self):
        """Fetch all available libraries to help user configure the bridge."""
        self._get_fresh_token()
        
        # Strategy 1: Try direct libraries endpoint
        try:
            response = self._make_request("GET", "/api/v1/libraries")
            if response and response.status_code == 200:
                libs = response.json()
                # Return standardized list
                return [{'id': l.get('id'), 'name': l.get('name'), 'path': l.get('root', {}).get('path') or l.get('path')} for l in libs]
        except Exception as e:
            logger.debug(f"Booklore: Failed to fetch /api/v1/libraries: {e}")

        # Strategy 2: Fallback - Scan a few books to find unique libraries
        try:
            logger.info("Booklore: Scanning books to discover libraries...")
            response = self._make_request("GET", "/api/v1/books?page=0&size=50")
            if response and response.status_code == 200:
                data = response.json()
                books = data if isinstance(data, list) else data.get('content', [])
                
                unique_libs = {}
                for b in books:
                    lid = b.get('libraryId')
                    if lid and lid not in unique_libs:
                        unique_libs[lid] = {
                            'id': lid,
                            'name': b.get('libraryName', 'Unknown Library'),
                            'path': 'Path not available in book scan'
                        }
                return list(unique_libs.values())
        except Exception as e:
            logger.error(f"❌ Booklore: Failed to discover libraries via book scan: {e}")
            
        return []

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
                logger.error(f"❌ Booklore: Failed to fetch page {page}")
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
            
            # Filter by libraryId if configured
            if self.target_library_id and current_batch:
                filtered_batch = []
                for b in current_batch:
                    lid = b.get('libraryId')
                    lname = b.get('libraryName', 'Unknown')

                    # Robust comparison (str vs int)
                    if lid is not None and str(lid) == str(self.target_library_id):
                        filtered_batch.append(b)
                    elif lid is None:
                        # Conservative: Keep if ID missing, verify later
                        filtered_batch.append(b)
                    else:
                        # Log exclusion at DEBUG
                        logger.debug(f"Booklore: Ignoring book '{b.get('title')}' in Library '{lname}' (ID: {lid})")
                current_batch = filtered_batch

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
            self._save_cache() # No-op now
            return True

        logger.info(f"📚 Booklore: Scan complete. Found {len(all_books_list)} total books.")

        # --- Pruning Stale Data ---
        if self.db and all_books_list:
            # 1. Map valid IDs to their live data for strict verification
            live_map = {str(b['id']): b for b in all_books_list if b.get('id')}
            
            # 2. Check existing cache for ghosts
            cached_filenames = list(self._book_cache.keys())
            stale_count = 0
            
            for fname in cached_filenames:
                book_info = self._book_cache[fname]
                bid = book_info.get('id')
                
                is_stale = False
                
                # Check 1: ID Validity
                if not bid or str(bid) not in live_map:
                    is_stale = True
                    logger.debug(f"   Pruning {fname}: ID {bid} not in live map")
                else:
                    # Check 2: Content Consistency
                    
                    # A. Filename Check
                    # Ideally, we compare the Live filename with the Cached filename.
                    # Problem 1: The Live API 'List' view might return empty filenames (Live: '').
                    # Problem 2: Cache Key 'fname' is lowercase, but real filename is in book_info['fileName'].
                    
                    live_book = live_map[str(bid)]
                    raw_live_filename = live_book.get('primaryFile', {}).get('fileName', live_book.get('fileName', ''))
                    # Clean filename to ensure we don't treat whitespace/control chars as valid names
                    live_filename = str(raw_live_filename).strip() if raw_live_filename else ''
                    
                    cached_real_filename = book_info.get('fileName', fname)
                    
                    # Only prune if we HAVE a valid, non-empty live filename to compare against
                    if live_filename:
                        # Strict check: If API returns explicit filename, it must match.
                        # We compare against the REAL cached filename (preserving case if possible)
                        # Normalize both sides to be safe (strip)
                        if live_filename != str(cached_real_filename).strip():
                             is_stale = True
                             # Use repr() to reveal any invisible characters/whitespace in debug logs
                             logger.debug(f"   Pruning {fname}: Filename mismatch. Live: {repr(raw_live_filename)} vs Cache: {repr(cached_real_filename)}")
                    else:
                        # B. Fallback Title Check (if filename is missing in List View)
                        # If titles differ significantly, it's likely a reused ID (Ghost)
                        live_title = live_book.get('title')
                        cached_title = book_info.get('title')
                        
                        if live_title and cached_title:
                            # Normalize for safety (ignore case/whitespace/symbols)
                            # This catches "The Book" vs "Another Book" (ID Reuse)
                            lt_norm = self._normalize_string(live_title)
                            ct_norm = self._normalize_string(cached_title)
                            
                            # Use a generous equality check to avoid false positives on minor edits
                            if lt_norm and ct_norm and lt_norm != ct_norm:
                                 is_stale = True
                                 logger.debug(f"   Pruning {fname}: Title mismatch (ID Reuse?). Live: '{live_title}' vs Cache: '{cached_title}'")

                if is_stale:
                    stale_count += 1
                    # Remove from Memory
                    self._book_cache.pop(fname, None)
                    if bid:
                        self._book_id_cache.pop(bid, None)
                    
                    # Remove from Database
                    try:
                        # Use the CACHE KEY (fname) which corresponds to the database `filename` column (lowercase)
                        self.db.delete_booklore_book(fname)
                    except Exception as e:
                        logger.error(f"❌ Failed to prune stale book {fname}: {e}")

            if stale_count > 0:
                logger.info(f"🧹 Booklore: Pruned {stale_count} stale books from database.")

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
        # self._save_cache() # DB is updated inside _process_book_detail
        return True

    def _process_book_detail(self, detail):
        """Process a book detail response and add to cache."""
        # Library ID Filter
        if self.target_library_id:
            lid = detail.get('libraryId')
            if lid is not None and str(lid) != str(self.target_library_id):
                return None

        primary_file = detail.get('primaryFile', {})
        filename = primary_file.get('fileName', detail.get('fileName', ''))
        filepath = primary_file.get('filePath', detail.get('filePath', ''))
        book_type = primary_file.get('bookType', detail.get('bookType', ''))
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
            'filePath': filepath,
            'title': title,
            'subtitle': subtitle,
            'authors': author_str,
            'bookType': book_type,
            'epubProgress': detail.get('epubProgress'),
            'pdfProgress': detail.get('pdfProgress'),
            'cbxProgress': detail.get('cbxProgress'),
            'koreaderProgress': detail.get('koreaderProgress'),
        }

        # Let's keep it consistent with what we see in database migration
        
        self._book_cache[filename.lower()] = book_info
        self._book_id_cache[detail['id']] = book_info

        # Persist to DB
        if self.db:
            try:
                from src.db.models import BookloreBook
                import json as pyjson
                
                b_model = BookloreBook(
                    filename=filename.lower(), # Store key as lowercase filename for consistency
                    title=title,
                    authors=author_str,
                    raw_metadata=pyjson.dumps(book_info)
                )
                self.db.save_booklore_book(b_model)
            except Exception as e:
                logger.error(f"❌ Failed to persist book {filename} to DB: {e}")

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
        logger.debug(f"Downloading book from {url}")

        try:
            response = self.session.get(url, headers=headers, timeout=60)
            
            # Fallback for newer Booklore versions or different configurations
            if response.status_code == 404:
                file_url = f"{self.base_url}/api/v1/books/{book_id}/file"
                logger.debug(f"404 on /download, trying fallback: {file_url}")
                response = self.session.get(file_url, headers=headers, timeout=60)

            if response.status_code != 200:
                logger.error(f"❌ Failed to download book: {response.status_code}")
                return None

            return response.content
        except Exception as e:
            logger.error(f"❌ Download error: {e}")
            return None

    @staticmethod
    def _to_progress_fraction(raw_pct):
        """Convert Booklore percentage (0-100) to fraction (0-1) safely."""
        if raw_pct in (None, ""):
            return 0.0
        try:
            return float(raw_pct) / 100.0
        except (TypeError, ValueError):
            return 0.0

    def _get_progress_by_book_id(self, book_id):
        """
        Get progress tuple for a specific Booklore book id.
        Returns: (pct_fraction, cfi) or (None, None) on failure.
        """
        response = self._make_request("GET", f"/api/v1/books/{book_id}")
        if not response or response.status_code != 200:
            return None, None

        data = response.json()
        book_type = str(
            data.get('primaryFile', {}).get('bookType')
            or data.get('bookType')
            or ''
        ).upper()
        if book_type == 'EPUB':
            progress = data.get('epubProgress') or {}
            return self._to_progress_fraction(progress.get('percentage', 0)), progress.get('cfi')
        if book_type == 'PDF':
            progress = data.get('pdfProgress') or {}
            return self._to_progress_fraction(progress.get('percentage', 0)), None
        if book_type == 'CBX':
            progress = data.get('cbxProgress') or {}
            return self._to_progress_fraction(progress.get('percentage', 0)), None
        return None, None

    def get_progress(self, ebook_filename):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            return None, None
        return self._get_progress_by_book_id(book['id'])

    def update_progress(self, ebook_filename, percentage, rich_locator: Optional[LocatorResult] = None):
        book = self.find_book_by_filename(ebook_filename)
        if not book:
            logger.debug(f"Booklore: Book not found: {ebook_filename}")
            return False

        book_id = book['id']
        book_type = (book.get('bookType') or '').upper()
        pct_display = percentage * 100

        clear_reset = book_type == 'EPUB' and percentage <= 0
        cfi = rich_locator.cfi if rich_locator and rich_locator.cfi else None

        payload_variants = []
        if book_type == 'EPUB':
            base_payload = {"bookId": book_id, "epubProgress": {"percentage": pct_display}}
            if clear_reset:
                # Booklore variants differ by version/build. Try common clear forms.
                payload_variants = [
                    ("null_cfi", {"bookId": book_id, "epubProgress": {"percentage": pct_display, "cfi": None}}),
                    ("no_cfi", base_payload),
                ]
            else:
                if cfi is not None:
                    base_payload["epubProgress"]["cfi"] = cfi
                    logger.debug(f"Booklore: Setting CFI: {cfi}")
                payload_variants = [("standard", base_payload)]
        elif book_type == 'PDF':
            payload_variants = [("standard", {"bookId": book_id, "pdfProgress": {"page": 1, "percentage": pct_display}})]
        elif book_type == 'CBX':
            payload_variants = [("standard", {"bookId": book_id, "cbxProgress": {"page": 1, "percentage": pct_display}})]
        else:
            logger.warning(f"Booklore: Unknown book type {book_type} for {sanitize_log_data(ebook_filename)}")
            return False

        last_status = "No response"
        for variant_name, payload in payload_variants:
            if clear_reset:
                logger.debug(f"Booklore: Clearing CFI for 0% reset (variant={variant_name})")

            response = self._make_request("POST", "/api/v1/books/progress", payload)
            if not response or response.status_code not in [200, 201, 204]:
                last_status = response.status_code if response else "No response"
                continue

            # For clear operations, verify Booklore actually persisted 0%.
            if clear_reset:
                verified_pct, _ = self._get_progress_by_book_id(book_id)
                if verified_pct is not None and verified_pct > 0.001:
                    logger.warning(
                        f"Booklore clear did not persist for {sanitize_log_data(ebook_filename)} "
                        f"(variant={variant_name}, observed={verified_pct * 100:.2f}%). Retrying..."
                    )
                    last_status = f"verify_failed:{verified_pct * 100:.2f}%"
                    continue

            logger.info(f"Booklore: {sanitize_log_data(ebook_filename)} -> {pct_display:.1f}%")

            # Update cache in-place instead of full library refresh
            try:
                cached = self._book_id_cache.get(book_id)
                if cached:
                    if book_type == 'EPUB':
                        if not cached.get('epubProgress'):
                            cached['epubProgress'] = {}
                        cached['epubProgress']['percentage'] = pct_display
                        if clear_reset:
                            cached['epubProgress']['cfi'] = ""
                        elif 'cfi' in payload.get('epubProgress', {}):
                            cached['epubProgress']['cfi'] = payload['epubProgress']['cfi']
                    elif book_type == 'PDF':
                        if not cached.get('pdfProgress'):
                            cached['pdfProgress'] = {}
                        cached['pdfProgress']['percentage'] = pct_display
                    elif book_type == 'CBX':
                        if not cached.get('cbxProgress'):
                            cached['cbxProgress'] = {}
                        cached['cbxProgress']['percentage'] = pct_display
                    logger.debug(f"Booklore: Cache updated in-place for book {book_id}")
            except Exception:
                logger.debug("Booklore: In-place cache update failed, will refresh on next read")
            return True

        logger.error(f"Booklore update failed: {last_status}")
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
                logger.warning(f"⚠️ Booklore: Book not found for shelf assignment: {sanitize_log_data(ebook_filename)}")
                return False

            # Get or create shelf
            shelves_response = self._make_request("GET", "/api/v1/shelves")
            if not shelves_response or shelves_response.status_code != 200:
                logger.error("❌ Failed to get Booklore shelves")
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
                    logger.error(f"❌ Failed to create Booklore shelf: {shelf_name}")
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
                logger.error(f"❌ Failed to assign book to shelf. Status: {assign_response.status_code if assign_response else 'No response'}")
                return False

        except Exception as e:
            logger.error(f"❌ Error adding book to Booklore shelf: {e}")
            return False

    def remove_from_shelf(self, ebook_filename, shelf_name=None):
        """Remove a book from a shelf."""
        if not shelf_name:
             shelf_name = os.environ.get("BOOKLORE_SHELF_NAME", "abs-kosync")

        try:
            # Find the book
            book = self.find_book_by_filename(ebook_filename)
            if not book:
                logger.warning(f"⚠️ Booklore: Book not found for shelf removal: {sanitize_log_data(ebook_filename)}")
                return False

            # Get shelf
            shelves_response = self._make_request("GET", "/api/v1/shelves")
            if not shelves_response or shelves_response.status_code != 200:
                logger.error("❌ Failed to get Booklore shelves")
                return False

            shelves = shelves_response.json()
            target_shelf = next((s for s in shelves if s.get('name') == shelf_name), None)

            if not target_shelf:
                logger.warning(f"⚠️ Shelf '{shelf_name}' not found")
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
                logger.error(f"❌ Failed to remove book from shelf. Status: {assign_response.status_code if assign_response else 'No response'}")
                return False

        except Exception as e:
            logger.error(f"❌ Error removing book from Booklore shelf: {e}")
            return False
