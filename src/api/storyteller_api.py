# [START FILE: abs-kosync-enhanced/storyteller_api.py]
import os
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
        self.base_url = os.environ.get("STORYTELLER_API_URL", "http://localhost:8001").rstrip('/')
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
            logger.error(f"Storyteller login error: {e}")
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
            logger.error(f"Storyteller API request failed: {e}")
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
        import re
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
        payload = {
            "timestamp": new_ts,
            "locator": {
                "locations": {
                    "totalProgression": float(percentage)
                }
            }
        }
        if rich_locator and rich_locator.href is not None:
            payload['locator']['href'] = rich_locator.href
            payload['locator']['type'] = "application/xhtml+xml"
            if rich_locator.css_selector is not None:
                payload['locator']['locations']['cssSelector'] = rich_locator.css_selector
        else:
            # Fallback to preserve existing href if we are just sending a % update
            try:
                r = self._make_request("GET", f"/api/v2/books/{book_uuid}/positions")
                if r and r.status_code == 200:
                    old = r.json().get('locator', {})
                    if old.get('href'): payload['locator']['href'] = old['href']
                    if old.get('type'): payload['locator']['type'] = old['type']
            except Exception: pass

        response = self._make_request("POST", f"/api/v2/books/{book_uuid}/positions", payload)
        if response and response.status_code == 204:
            logger.info(f"✅ Storyteller API: {book_uuid[:8]}... → {percentage:.1%} (TS: {new_ts})")
            return True
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
             logger.info(f"🏷️ Added '{sanitize_log_data(ebook_filename)}' to Storyteller Collection: {collection_name}")
             return True
        # Backup strategy (singular)
        fallback = f"/api/v2/collections/{col_uuid}/books"
        r_back = self._make_request("POST", fallback, {"books": [book_uuid]})
    def search_books(self, query: str) -> list:
        """Search for books in Storyteller."""
        response = self._make_request("GET", "/api/v2/books", None)
        if response and response.status_code == 200:
            all_books = response.json()
            # Client-side filtering since API doesn't seem to support search query param
            query = query.lower()
            results = []
            for book in all_books:
                if query in book.get('title', '').lower() or \
                   any(query in author.get('name', '').lower() for author in book.get('authors', [])):
                    results.append({
                        'uuid': book.get('uuid') or book.get('id'),
                        'title': book.get('title'),
                        'authors': [a.get('name') for a in book.get('authors', [])],
                        'cover_url': f"/api/v2/books/{book.get('uuid') or book.get('id')}/cover" # Proxy might be needed
                    })
            return results
        return []

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
            logger.info(f"Attempting download from {url}")
            with self.session.get(url, headers=headers, params={"format": "readaloud"}, stream=True, timeout=60) as r:
                if r.status_code == 200:
                    with open(output_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192): 
                            f.write(chunk)
                    logger.info(f"✅ Downloaded Storyteller artifact for {book_uuid} to {output_path}")
                    return True
                else:
                    logger.warning(f"Storyteller API download failed: {r.status_code} - {r.text[:200]}")
        except Exception as e:
            logger.warning(f"API download raised exception: {e}")

        # Fallback: Local File Copy
        try:
            # 1. Get Book Details for Filepath
            r_details = self._make_request("GET", f"/api/v2/books/{book_uuid}")
            if not r_details or r_details.status_code != 200:
                logger.error(f"Failed to fetch book details for fallback: {r_details.status_code if r_details else 'No Response'}")
                raise Exception("API download failed and could not fetch details for fallback.")

            book_data = r_details.json()
            # Check readaloud object first, then root filepath
            readaloud = book_data.get('readaloud', {})
            source_path = readaloud.get('filepath')
            
            if not source_path:
                logger.error("No filepath found in book details for fallback.")
                raise Exception("No filepath in book details")

            # 2. Map Path
            # Mapping: /ebooks -> /storyteller/library
            # This should ideally be configurable, but hardcoding for this fix based on known setup
            local_path_str = source_path
            if source_path.startswith("/ebooks"):
                local_path_str = source_path.replace("/ebooks", "/storyteller/library", 1)
            
            local_path = Path(local_path_str)
            
            logger.info(f"Attempting local fallback from: {local_path}")
            
            if local_path.exists():
                import shutil
                shutil.copy2(local_path, output_path)
                logger.info(f"✅ Downloaded (via Local Copy) Storyteller artifact for {book_uuid}")
                return True
            else:
                 logger.error(f"Local fallback file not found: {local_path}")
                 # Try unmapped?
                 if Path(source_path).exists():
                     shutil.copy2(source_path, output_path)
                     logger.info(f"✅ Downloaded (via Direct Path) Storyteller artifact")
                     return True
                 
                 raise Exception(f"File not found at {local_path} or {source_path}")

        except Exception as e:
            logger.error(f"Failed to download Storyteller book {book_uuid} (API & Fallback): {e}")
            raise e

class StorytellerDBWithAPI:
    def __init__(self):
        self.api_client = None
        self.db_fallback = None

        api_url = os.environ.get("STORYTELLER_API_URL")
        api_user = os.environ.get("STORYTELLER_USER")
        api_pass = os.environ.get("STORYTELLER_PASSWORD")

        if api_url and api_user and api_pass:
            self.api_client = StorytellerAPIClient()
            if self.api_client.check_connection():
                logger.info("Using Storyteller REST API for sync")
            else:
                self.api_client = None
        if not self.api_client:
            try:
                from storyteller_db import StorytellerDB
                self.db_fallback = StorytellerDB()
                if not self.db_fallback.is_configured():
                    self.db_fallback = None
            except Exception: pass

    def is_configured(self) -> bool:
        return bool(self.api_client or self.db_fallback)

    def check_connection(self) -> bool:
        if self.api_client: return self.api_client.check_connection()
        elif self.db_fallback: return self.db_fallback.check_connection()
        return False

    def find_book_by_title(self, ebook_filename: str) -> Optional[Dict]:
        """Find book by title/filename. Delegates to API client if available."""
        if self.api_client:
            return self.api_client.find_book_by_title(ebook_filename)
        return None  # DB fallback doesn't support title lookup

    def clear_cache(self):
        """Call at start of each sync cycle to refresh caches."""
        if self.api_client:
            self.api_client.clear_cache()

    def get_progress(self, ebook_filename: str):
        # Legacy Wrapper
        pct, ts, _, _ = self.get_progress_with_fragment(ebook_filename)
        return pct, ts

    def get_progress_with_fragment(self, ebook_filename: str):
        # --- FIXED: Return real fragment data ---
        if self.api_client:
            return self.api_client.get_progress_by_filename(ebook_filename)
        elif self.db_fallback:
            return self.db_fallback.get_progress_with_fragment(ebook_filename)
        return None, None, None, None

    def get_all_positions_bulk(self) -> dict:
        if self.api_client:
            return self.api_client.get_all_positions_bulk()
        # SQLite fallback - iterate through books
        return {}

    def update_progress(self, ebook_filename: str, percentage: float, rich_locator: LocatorResult = None) -> bool:
        if self.api_client: return self.api_client.update_progress_by_filename(ebook_filename, percentage, rich_locator)
        elif self.db_fallback: return self.db_fallback.update_progress(ebook_filename, percentage, rich_locator)
        return False

    def update_position(self, book_uuid: str, percentage: float, rich_locator: LocatorResult = None) -> bool:
        if self.api_client: return self.api_client.update_position(book_uuid, percentage, rich_locator)
        return False

    def get_recent_activity(self, hours: int = 24, min_progress: float = 0.01):
        if self.db_fallback: return self.db_fallback.get_recent_activity(hours, min_progress)
        return []

    def add_to_collection(self, ebook_filename: str):
        if self.api_client: self.api_client.add_to_collection(ebook_filename)
        elif self.db_fallback: self.db_fallback.add_to_collection(ebook_filename)

    def search_books(self, query: str) -> list:
        if self.api_client: return self.api_client.search_books(query)
        return []

    def download_book(self, book_uuid: str, output_path: Path) -> bool:
        if self.api_client: return self.api_client.download_book(book_uuid, output_path)
        return False

def create_storyteller_client():
    return StorytellerDBWithAPI()
# [END FILE]