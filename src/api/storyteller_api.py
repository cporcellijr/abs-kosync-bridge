# [START FILE: abs-kosync-enhanced/storyteller_api.py]
import os
import re
import time
import logging
import requests
from typing import Optional, Dict, Tuple
from pathlib import Path

from src.utils.logging_utils import sanitize_log_data
from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)

class StorytellerAPIClient:
    def __init__(self):
        raw_url = os.environ.get("STORYTELLER_API_URL", "http://localhost:8001").rstrip('/')
        if raw_url and not raw_url.lower().startswith(('http://', 'https://')):
            raw_url = f"http://{raw_url}"
        self.base_url = raw_url
        self.username = os.environ.get("STORYTELLER_USER")
        self.password = os.environ.get("STORYTELLER_PASSWORD")
        self._book_cache: Dict[str, Dict] = {}
        self._cache_timestamp = 0
        self._token = None
        self._token_timestamp = 0
        self._token_max_age = 30
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._filename_to_book_cache = {}  # Cache filename -> book mapping

    def clear_cache(self):
        """Call at start of each sync cycle to refresh."""
        self._filename_to_book_cache = {}
        self._book_cache = {}

    def is_configured(self):
        enabled_val = os.environ.get("STORYTELLER_ENABLED", "").lower()
        if enabled_val == 'false':
            return False
        return bool(self.username and self.password)

    def _get_fresh_token(self) -> Optional[str]:
        if self._token and (time.time() - self._token_timestamp) < self._token_max_age:
            return self._token
        if not self.username or not self.password:
            # logger.warning("Storyteller API: No credentials configured")
            return None
        try:
            response = requests.post(
                f"{self.base_url}/api/token",
                data={"username": self.username, "password": self.password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                self._token = data.get("access_token")
                self._token_timestamp = time.time()
                return self._token
        except Exception as e:
            logger.error(f"❌ Storyteller login error: {e}")
        return None

    def _make_request(self, method: str, endpoint: str, json_data: dict = None) -> Optional[requests.Response]:
        token = self._get_fresh_token()
        if not token: return None
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            url = f"{self.base_url}{endpoint}"
            if method.upper() == "GET":
                response = self.session.get(url, headers=headers, timeout=10)
            elif method.upper() == "POST":
                response = self.session.post(url, headers=headers, json=json_data, timeout=10)
            elif method.upper() == "PUT":
                response = self.session.put(url, headers=headers, json=json_data, timeout=10)
            else: return None

            if response.status_code == 401:
                self._token = None
                token = self._get_fresh_token()
                if not token: return None
                headers["Authorization"] = f"Bearer {token}"
                if method.upper() == "GET":
                    response = self.session.get(url, headers=headers, timeout=10)
                elif method.upper() == "POST":
                    response = self.session.post(url, headers=headers, json=json_data, timeout=10)
                elif method.upper() == "PUT":
                    response = self.session.put(url, headers=headers, json=json_data, timeout=10)
            return response
        except Exception as e:
            logger.error(f"❌ Storyteller API request failed ('{method}' '{endpoint}'): {e}")
            return None

    def check_connection(self) -> bool:
        return bool(self._get_fresh_token())

    def _refresh_book_cache(self) -> bool:
        response = self._make_request("GET", "/api/v2/books")
        if response and response.status_code == 200:
            books = response.json()
            self._book_cache = {}
            for book in books:
                title = book.get('title', '').lower()
                self._book_cache[title] = {
                    'id': book.get('id'),
                    'uuid': book.get('uuid'),
                    'title': book.get('title')
                }
            self._cache_timestamp = time.time()
            return True
        return False

    def find_book_by_title(self, ebook_filename: str) -> Optional[Dict]:
        if time.time() - self._cache_timestamp > 3600: self._refresh_book_cache()
        if not self._book_cache: self._refresh_book_cache()

        stem = Path(ebook_filename).stem.lower()
        clean_stem = re.sub(r'\s*\([^)]*\)\s*$', '', stem)
        clean_stem = re.sub(r'\s*\[[^\]]*\]\s*$', '', clean_stem)
        clean_stem = clean_stem.strip().lower()

        clean_stem = clean_stem.strip().lower()

        # Check cache first
        cache_key = ebook_filename.lower()
        if cache_key in self._filename_to_book_cache:
            return self._filename_to_book_cache[cache_key]

        if clean_stem in self._book_cache: 
            self._filename_to_book_cache[cache_key] = self._book_cache[clean_stem]
            return self._book_cache[clean_stem]

        for title, book_info in self._book_cache.items():
            if clean_stem in title or title in clean_stem: 
                self._filename_to_book_cache[cache_key] = book_info
                return book_info

        stem_words = set(clean_stem.split())
        for title, book_info in self._book_cache.items():
            title_words = set(title.split())
            common = stem_words & title_words
            if len(common) >= min(len(stem_words), len(title_words)) * 0.7:
                self._filename_to_book_cache[cache_key] = book_info
                return book_info
        return None

    def get_position_details(self, book_uuid: str) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str]]:
        """
        Returns: (percentage, timestamp, href, fragment_id)
        """
        response = self._make_request("GET", f"/api/v2/books/{book_uuid}/positions")
        if response and response.status_code == 200:
            data = response.json()
            locator = data.get('locator', {})
            locations = locator.get('locations', {})

            pct = float(locations.get('totalProgression', 0))
            ts = int(data.get('timestamp', 0))

            # --- EXTRACT PRECISION DATA ---
            href = locator.get('href') # e.g. "OEBPS/Text/part0000.html"
            fragment = None
            if locations.get('fragments') and len(locations['fragments']) > 0:
                fragment = locations['fragments'][0] # e.g. "id628-sentence94"

            return pct, ts, href, fragment

        return None, None, None, None

    def get_all_positions_bulk(self) -> dict:
        """Fetch all book positions in one pass. Returns {title_lower: {pct, ts, href, frag, uuid}}"""
        if not self._book_cache:
            self._refresh_book_cache()
        
        positions = {}
        for title, book in self._book_cache.items():
            uuid = book.get('uuid')
            if not uuid:
                continue
            pct, ts, href, frag = self.get_position_details(uuid)
            if pct is not None:
                positions[title.lower()] = {
                    'pct': pct, 'ts': ts, 'href': href, 'frag': frag, 'uuid': uuid
                }
        return positions

    def update_position(self, book_uuid: str, percentage: float, rich_locator: LocatorResult = None) -> bool:
        new_ts = int(time.time() * 1000)
        
        # Base Payload with UUID (critical)
        payload = {
            "uuid": book_uuid,
            "timestamp": new_ts,
            "locator": {
                "href": "",
                "type": "application/xhtml+xml",
                "locations": {
                    "totalProgression": float(percentage)
                }
            }
        }

        if rich_locator:
            # 1. Href
            if rich_locator.href:
                payload['locator']['href'] = rich_locator.href

            # 2. CSS Selector
            if rich_locator.css_selector:
                payload['locator']['locations']['cssSelector'] = rich_locator.css_selector
                
            # 3. Fragments (List)
            if rich_locator.fragment:
                payload['locator']['locations']['fragments'] = [rich_locator.fragment]
            elif rich_locator.fragments: # Check if list already populated (future proof)
                payload['locator']['locations']['fragments'] = rich_locator.fragments
                
            # 4. Chapter Progress (Critical for Storyteller)
            if rich_locator.chapter_progress is not None:
                payload['locator']['locations']['progression'] = rich_locator.chapter_progress
            else:
                 # Fallback: if we don't have chapter progress, maybe default to 0 or omit?
                 # Storyteller logs show it as distinct. 
                 # If we omit, it might calculate it? 
                 # For now, let's leave it out if None to avoid sending null.
                 pass

            # 5. Position (Global Integer)
            if rich_locator.match_index is not None:
                payload['locator']['locations']['position'] = rich_locator.match_index
                
            # 6. CFI
            if rich_locator.cfi:
                payload['locator']['locations']['cfi'] = rich_locator.cfi

        else:
            # Fallback for simple percentage update (legacy)
            try:
                r = self._make_request("GET", f"/api/v2/books/{book_uuid}/positions")
                if r and r.status_code == 200:
                    old = r.json().get('locator', {})
                    if old.get('href'): payload['locator']['href'] = old['href']
                    if old.get('type'): payload['locator']['type'] = old['type']
            except Exception: pass

        response = self._make_request("POST", f"/api/v2/books/{book_uuid}/positions", payload)
        
        if response:
            if response.status_code == 204:
                logger.info(f"✅ Storyteller API: {book_uuid[:8]}... -> {percentage:.1%} (TS: {new_ts})")
                return True
            elif response.status_code == 409:
                logger.warning(f"⚠️ Storyteller rejected update for '{book_uuid[:8]}...': Timestamp older than server state (Ignored)")
                return True # Treat as 'handled' to prevent retry loops
            else:
                logger.warning(f"⚠️ Storyteller API error: {response.status_code} - {response.text[:100]}")
        
        return False

    def get_progress_by_filename(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str]]:
        book = self.find_book_by_title(ebook_filename)
        if not book: return None, None, None, None
        return self.get_position_details(book['uuid'])

    def update_progress_by_filename(self, ebook_filename: str, percentage: float, rich_locator: LocatorResult = None) -> bool:
        book = self.find_book_by_title(ebook_filename)
        if not book: return False
        return self.update_position(book['uuid'], percentage, rich_locator)

    def add_to_collection(self, ebook_filename: str, collection_name: str = None) -> bool:
        if not collection_name:
            collection_name = os.environ.get("STORYTELLER_COLLECTION_NAME", "Synced with KOReader")
        book = self.find_book_by_title(ebook_filename)
        if not book: return False

        # 1. Get Collections
        r = self._make_request("GET", "/api/v2/collections")
        if not r or r.status_code != 200: return False
        collections = r.json()
        target_col = next((c for c in collections if c.get('name') == collection_name), None)

        # 2. Create if missing
        if not target_col:
            r_create = self._make_request("POST", "/api/v2/collections", {"name": collection_name})
            if r_create and r_create.status_code in [200, 201]:
                target_col = r_create.json()
            else: return False

        col_uuid = target_col.get('uuid') or target_col.get('id')
        book_uuid = book.get('uuid') or book.get('id')

        # 3. Add book (Batch Endpoint from route(2).ts)
        endpoint = "/api/v2/collections/books"
        payload = {"collections": [col_uuid], "books": [book_uuid]}
        r_add = self._make_request("POST", endpoint, payload)
        if r_add and r_add.status_code in [200, 204]:
             logger.info(f"🏷️ Added '{sanitize_log_data(ebook_filename)}' to Storyteller Collection: '{collection_name}'")
             return True
        # Backup strategy (singular)
        fallback = f"/api/v2/collections/{col_uuid}/books"
        r_back = self._make_request("POST", fallback, {"books": [book_uuid]})
        
    def add_to_collection_by_uuid(self, book_uuid: str, collection_name: str = None) -> bool:
        if not collection_name:
            collection_name = os.environ.get("STORYTELLER_COLLECTION_NAME", "Synced with KOReader")

        # 1. Get Collections
        r = self._make_request("GET", "/api/v2/collections")
        if not r or r.status_code != 200: return False
        collections = r.json()
        target_col = next((c for c in collections if c.get('name') == collection_name), None)

        # 2. Create if missing
        if not target_col:
            r_create = self._make_request("POST", "/api/v2/collections", {"name": collection_name})
            if r_create and r_create.status_code in [200, 201]:
                target_col = r_create.json()
            else: return False

        col_uuid = target_col.get('uuid') or target_col.get('id')

        # 3. Add book (Batch Endpoint from route(2).ts)
        endpoint = "/api/v2/collections/books"
        payload = {"collections": [col_uuid], "books": [book_uuid]}
        r_add = self._make_request("POST", endpoint, payload)
        if r_add and r_add.status_code in [200, 204]:
             logger.info(f"🏷️ Added '{book_uuid[:8]}' to Storyteller Collection: '{collection_name}'")
             return True
        # Backup strategy (singular)
        fallback = f"/api/v2/collections/{col_uuid}/books"
        r_back = self._make_request("POST", fallback, {"books": [book_uuid]})
        return bool(r_back and r_back.status_code in [200, 204])
    def search_books(self, query: str) -> list:
        """Search for books in Storyteller."""
        response = self._make_request("GET", "/api/v2/books", None)
        if response and response.status_code == 200:
            all_books = response.json()
            stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'is'}
            query_lower = query.lower()
            query_tokens = [w for w in re.split(r'\W+', query_lower) if w and w not in stopwords]

            if not query_tokens:
                return []

            query_set = set(query_tokens)
            results = []
            for book in all_books:
                title = book.get('title', '')
                author_names = ' '.join(a.get('name', '') for a in book.get('authors', []))
                searchable = f"{title} {author_names}".lower()

                if len(query_tokens) == 1:
                    matched = query_tokens[0] in searchable
                else:
                    searchable_tokens = set(w for w in re.split(r'\W+', searchable) if w and w not in stopwords)
                    overlap = len(query_set & searchable_tokens)
                    matched = overlap >= min(len(query_set), len(searchable_tokens)) * 0.5

                if matched:
                    results.append({
                        'uuid': book.get('uuid') or book.get('id'),
                        'title': title,
                        'authors': [a.get('name') for a in book.get('authors', [])],
                        'cover_url': f"/api/v2/books/{book.get('uuid') or book.get('id')}/cover"
                    })
            return results
        return []

    def find_book_by_staged_path(self, staged_folder_name: str, staged_epub_name: str) -> Optional[str]:
        """Find a book UUID by matching its ebook file path suffix.

        Matches against the path suffix '/{folder}/{epub}' to avoid
        dependence on absolute library paths which vary per user.
        Returns the book UUID if found, None otherwise.
        """
        expected_suffix = f"/{staged_folder_name}/{staged_epub_name}"
        response = self._make_request("GET", "/api/v2/books", None)
        if not response or response.status_code != 200:
            return None

        all_books = response.json()
        for book in all_books:
            if self._check_path_match(book, expected_suffix):
                return book.get('uuid') or book.get('id')

        return None

    def _check_path_match(self, book: dict, expected_suffix: str) -> bool:
        """Check all fields in a book object for a path ending match."""
        for key, val in book.items():
            if isinstance(val, str) and val.endswith(expected_suffix):
                logger.info(f"⚡ Forge: Path match on '{key}': {val}")
                return True
            if isinstance(val, dict):
                for sub_key, sub_val in val.items():
                    if isinstance(sub_val, str) and sub_val.endswith(expected_suffix):
                        logger.info(f"⚡ Forge: Path match on '{key}.{sub_key}': {sub_val}")
                        return True

        return False

    def download_book(self, book_uuid: str, output_path: Path) -> bool:
        """Download the processed EPUB3 artifact."""
        # Endpoint: GET /api/v2/books/{uuid}/files?format=readaloud
        # Note: 'readaloud' format usually implies the processed EPUB3
        url = f"{self.base_url}/api/v2/books/{book_uuid}/files"
        # We need to manually construct the request to handle streaming
        token = self._get_fresh_token()
        if not token: return False
        headers = {"Authorization": f"Bearer {token}"}
        
        # Try API Download First
        try:
            logger.info(f"⚡ Attempting download from '{url}'")
            with self.session.get(url, headers=headers, params={"format": "readaloud"}, stream=True, timeout=60) as r:
                if r.status_code == 200:
                    with open(output_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192): 
                            f.write(chunk)
                    logger.info(f"✅ Downloaded Storyteller artifact for '{book_uuid}' to '{output_path}'")
                    return True
                else:
                    logger.warning(f"⚠️ Storyteller API download failed: {r.status_code} - {r.text[:200]}")
        except Exception as e:
            logger.warning(f"⚠️ API download raised exception: {e}")

    def trigger_processing(self, book_uuid: str) -> bool:
        """Trigger the Storyteller processing for a book."""
        try:
            response = self._make_request("POST", f"/api/v2/books/{book_uuid}/process", {})
            if response and response.status_code in [200, 201, 202, 204]:
                logger.info(f"✅ Triggered Storyteller processing for '{book_uuid}'")
                return True
            else:
                logger.warning(f"⚠️ Failed to trigger processing: {response.status_code if response else 'No Resp'}")
                return False
        except Exception as e:
            logger.error(f"❌ Error triggering processing: {e}")
            return False

            return False

    def get_book_details(self, book_uuid: str) -> Optional[Dict]:
        """Fetch full book details from Storyteller API."""
        try:
            response = self._make_request("GET", f"/api/v2/books/{book_uuid}")
            if response and response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.error(f"❌ Error fetching book details: {e}")
        return None

        # Fallback: Local File Copy
        try:
            # 1. Get Book Details for Filepath
            r_details = self._make_request("GET", f"/api/v2/books/{book_uuid}")
            if not r_details or r_details.status_code != 200:
                logger.error(f"❌ Failed to fetch book details for fallback: {r_details.status_code if r_details else 'No Response'}")
                raise Exception("API download failed and could not fetch details for fallback.")

            book_data = r_details.json()
            # Check readaloud object first, then root filepath
            readaloud = book_data.get('readaloud', {})
            source_path = readaloud.get('filepath')
            
            if not source_path:
                logger.error("❌ No filepath found in book details for fallback")
                raise Exception("No filepath in book details")

            # 2. Map Path
            # Mapping: /ebooks -> /storyteller/library
            # This should ideally be configurable, but hardcoding for this fix based on known setup
            local_path_str = source_path
            if source_path.startswith("/ebooks"):
                local_path_str = source_path.replace("/ebooks", "/storyteller/library", 1)
            
            local_path = Path(local_path_str)
            
            logger.info(f"🔄 Attempting local fallback from: '{local_path}'")
            
            if local_path.exists():
                import shutil
                shutil.copy2(local_path, output_path)
                logger.info(f"✅ Downloaded (via Local Copy) Storyteller artifact for '{book_uuid}'")
                return True
            else:
                 logger.error(f"❌ Local fallback file not found: '{local_path}'")
                 # Try unmapped?
                 if Path(source_path).exists():
                     shutil.copy2(source_path, output_path)
                     logger.info(f"✅ Downloaded (via Direct Path) Storyteller artifact")
                     return True
                 
                 raise Exception(f"File not found at {local_path} or {source_path}")

        except Exception as e:
            logger.error(f"❌ Failed to download Storyteller book '{book_uuid}' (API & Fallback): {e}")
            raise e

    def get_progress(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int]]:
        """Legacy compatibility wrapper."""
        pct, ts, _, _ = self.get_progress_by_filename(ebook_filename)
        return pct, ts

    def get_progress_with_fragment(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int], Optional[str], Optional[str]]:
        """Legacy compatibility wrapper."""
        return self.get_progress_by_filename(ebook_filename)

def create_storyteller_client():
    return StorytellerAPIClient()
# [END FILE]