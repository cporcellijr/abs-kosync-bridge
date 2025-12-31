# [START FILE: abs-kosync-enhanced/storyteller_api.py]
import os
import time
import logging
import requests
from typing import Optional, Dict, Tuple
from pathlib import Path

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
    
    def _get_fresh_token(self) -> Optional[str]:
        if self._token and (time.time() - self._token_timestamp) < self._token_max_age:
            return self._token
        if not self.username or not self.password:
            logger.warning("Storyteller API: No credentials configured")
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
        
        if clean_stem in self._book_cache: return self._book_cache[clean_stem]
        
        for title, book_info in self._book_cache.items():
            if clean_stem in title or title in clean_stem: return book_info
        
        stem_words = set(clean_stem.split())
        for title, book_info in self._book_cache.items():
            title_words = set(title.split())
            common = stem_words & title_words
            if len(common) >= min(len(stem_words), len(title_words)) * 0.7:
                return book_info
        return None
    
    def get_position(self, book_uuid: str) -> Tuple[Optional[float], Optional[int]]:
        response = self._make_request("GET", f"/api/books/{book_uuid}/positions")
        if response and response.status_code == 200:
            data = response.json()
            return float(data.get('locator', {}).get('locations', {}).get('totalProgression', 0)), int(data.get('timestamp', 0))
        return None, None
    
    def update_position(self, book_uuid: str, percentage: float, rich_locator: dict = None) -> bool:
        """
        Update reading position via Storyteller API.
        Prioritizes 'rich_locator' (href/cssSelector) to prevent 'undefined' href crashes.
        """
        payload = {
            "locator": {
                "locations": {
                    "totalProgression": float(percentage)
                }
            }
        }

        # 1. Use authoritative Rich Locator if available
        if rich_locator and rich_locator.get('href'):
            payload['locator']['href'] = rich_locator['href']
            payload['locator']['type'] = "application/xhtml+xml"
            if rich_locator.get('cssSelector'):
                payload['locator']['locations']['cssSelector'] = rich_locator['cssSelector']
        
        # 2. Fallback: Fetch existing position to preserve href
        else:
            try:
                r = self._make_request("GET", f"/api/books/{book_uuid}/positions")
                if r and r.status_code == 200:
                    old = r.json().get('locator', {})
                    if old.get('href'): payload['locator']['href'] = old['href']
                    if old.get('type'): payload['locator']['type'] = old['type']
            except Exception: pass
        
        response = self._make_request("POST", f"/api/books/{book_uuid}/positions", payload)
        if response and response.status_code == 204:
            logger.info(f"✅ Storyteller API: {book_uuid[:8]}... → {percentage:.1%}")
            return True
        else:
            logger.error(f"Storyteller update failed: {response.status_code if response else 'No Resp'}")
            return False
    
    def get_progress_by_filename(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int]]:
        book = self.find_book_by_title(ebook_filename)
        if not book: return None, None
        return self.get_position(book['uuid'])
    
    def update_progress_by_filename(self, ebook_filename: str, percentage: float, rich_locator: dict = None) -> bool:
        book = self.find_book_by_title(ebook_filename)
        if not book: return False
        return self.update_position(book['uuid'], percentage, rich_locator)


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
            except Exception: pass
    
    def check_connection(self) -> bool:
        if self.api_client: return self.api_client.check_connection()
        elif self.db_fallback: return self.db_fallback.check_connection()
        return False
    
    def get_progress(self, ebook_filename: str) -> Tuple[Optional[float], Optional[int]]:
        if self.api_client: return self.api_client.get_progress_by_filename(ebook_filename)
        elif self.db_fallback: return self.db_fallback.get_progress(ebook_filename)
        return None, None
    
    def get_progress_with_fragment(self, ebook_filename: str):
        if self.db_fallback: return self.db_fallback.get_progress_with_fragment(ebook_filename)
        pct, ts = self.get_progress(ebook_filename)
        return pct, ts, None, None
    
    def update_progress(self, ebook_filename: str, percentage: float, rich_locator: dict = None) -> bool:
        if self.api_client: return self.api_client.update_progress_by_filename(ebook_filename, percentage, rich_locator)
        elif self.db_fallback: return self.db_fallback.update_progress(ebook_filename, percentage, rich_locator)
        return False
    
    def get_recent_activity(self, hours: int = 24, min_progress: float = 0.01):
        if self.db_fallback: return self.db_fallback.get_recent_activity(hours, min_progress)
        return []
    
    def add_to_collection(self, ebook_filename: str):
        if self.db_fallback: self.db_fallback.add_to_collection(ebook_filename)

def create_storyteller_client():
    return StorytellerDBWithAPI()
# [END FILE]