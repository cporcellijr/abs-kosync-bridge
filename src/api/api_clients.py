# [START FILE: abs-kosync-enhanced/api_clients.py]
import os
import requests
import logging
import time

from src.utils.kosync_headers import hash_kosync_key, kosync_auth_headers
from src.utils.logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

class ABSClient:
    def __init__(self):
        # Kept your variable names (ABS_SERVER / ABS_KEY)
        self.base_url = os.environ.get("ABS_SERVER", "").rstrip('/')
        self.token = os.environ.get("ABS_KEY")
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.timeout = 30

    def is_configured(self):
        """Check if ABS is configured with URL and token."""
        return bool(self.base_url and self.token)

    def check_connection(self):
        # Verify configuration first
        if not self.is_configured():
            logger.warning("⚠️ Audiobookshelf not configured (skipping)")
            return False

        url = f"{self.base_url}/api/me"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                # If this is the first container start, show INFO for visibility; otherwise use DEBUG
                first_run_marker = '/data/.first_run_done'
                try:
                    first_run = not os.path.exists(first_run_marker)
                except Exception:
                    first_run = False

                if first_run:
                    logger.info(f"✅ Connected to Audiobookshelf as user: {r.json().get('username', 'Unknown')}")
                    try:
                        open(first_run_marker, 'w').close()
                    except Exception:
                        pass
                return True
            else:
                # Keep failure visible as warning
                logger.warning(f"❌ Audiobookshelf Connection Failed: {r.status_code} - {sanitize_log_data(r.text)}")
                return False
        except requests.exceptions.ConnectionError:
            logger.warning(f"❌ Could not connect to Audiobookshelf at {self.base_url}. Check URL and Docker Network.")
            return False
        except Exception as e:
            logger.warning(f"❌ Audiobookshelf Error: {e}")
            return False

    def get_all_audiobooks(self):
        lib_url = f"{self.base_url}/api/libraries"
        try:
            r = self.session.get(lib_url, timeout=self.timeout)
            if r.status_code != 200: return []
            libraries = r.json().get('libraries', [])
            all_audiobooks = []
            for lib in libraries:
                r_items = self.get_audiobooks_for_lib(lib['id'])
                all_audiobooks.extend(r_items)
            return all_audiobooks
        except Exception as e:
            logger.error(f"Exception fetching audiobooks: {e}")
            return []

    def get_audiobooks_for_lib(self, lib: str):
        items_url = f"{self.base_url}/api/libraries/{lib}/items"
        params = {"mediaType": "audiobook"}
        r_items = self.session.get(items_url, params=params, timeout=self.timeout)
        if r_items.status_code == 200:
            return r_items.json().get('results', [])
        logger.warning("⚠️ ABS - Failed to fetch audiobooks for library " + lib)
        return []

    def get_audio_files(self, item_id):
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                files = []
                # Return list of dicts with stream_url and ext (for transcriber)
                audio_files = data.get('media', {}).get('audioFiles', [])
                audio_files.sort(key=lambda x: (x.get('disc', 0) or 0, x.get('track', 0) or 0))

                for af in audio_files:
                    stream_url = f"{self.base_url}/api/items/{item_id}/file/{af['ino']}?token={self.token}"
                    # Return dict with stream URL and extension (default to mp3)
                    files.append({
                        "stream_url": stream_url,
                        "ext": af.get("ext", "mp3")
                    })
                return files
            return []
        except Exception as e:
            logger.error(f"Error getting audio files: {e}")
            return []

    def get_ebook_files(self, item_id):
        """Get ebook files for an item (from libraryFiles)."""
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                library_files = data.get('libraryFiles', [])
                ebook_files = []
                
                for f in library_files:
                    ext = f.get('metadata', {}).get('ext') or f.get('ext') or ""
                    ext = ext.lower().replace('.', '')
                    if ext in ['epub', 'mobi', 'pdf', 'azw3']:
                         stream_url = f"{self.base_url}/api/items/{item_id}/file/{f['ino']}?token={self.token}"
                         ebook_files.append({
                             "stream_url": stream_url,
                             "ext": ext,
                             "ino": f['ino']
                         })
                return ebook_files
            return []
        except Exception as e:
            logger.error(f"Error getting ebook files: {e}")
            return []

    def search_ebooks(self, query):
        """Search for ebooks across all book libraries."""
        results = []
        try:
            # Get all libraries first
            r_libs = self.session.get(f"{self.base_url}/api/libraries", timeout=self.timeout)
            if r_libs.status_code != 200: return []
            
            libraries = r_libs.json().get('libraries', [])
            # Search ALL libraries to support mixed content (e.g. ebooks in audiobook libraries)
            # book_libs = [l for l in libraries if l.get('mediaType') == 'book']
            
            for lib in libraries:
                search_url = f"{self.base_url}/api/libraries/{lib['id']}/search"
                params = {'q': query, 'limit': 3}
                r = self.session.get(search_url, params=params, timeout=self.timeout)
                if r.status_code == 200:
                    items = r.json().get('results', [])
                    if items:
                         logger.debug(f"   ABS Search: Found {len(items)} hits in library '{lib.get('name')}'")
                    for item in items:
                        match = item.get('match', {})
                        results.append({
                            "id": item.get('id'),
                            "title": match.get('title'),
                            "author": match.get('author'),
                            "libraryId": lib['id'],
                            "source": "ABS",
                            "ext": "epub" # Assumption, refined later by get_ebook_files
                        })
            return results
        except Exception as e:
            logger.error(f"Error searching ABS ebooks: {e}")
            return []

    def download_file(self, stream_url, output_path):
        """Download file from stream_url to output_path."""
        try:
            logger.info(f"⬇️ ABS: Downloading file from {stream_url}...")
            with self.session.get(stream_url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(output_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                return True
            return False
        except Exception as e:
            logger.error(f"❌ ABS Download failed: {e}")
            if os.path.exists(output_path): os.remove(output_path)
            return False

    def get_item_details(self, item_id):
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200: return r.json()
        except Exception:
            pass
        return None

    def get_progress(self, item_id):
        url = f"{self.base_url}/api/me/progress/{item_id}"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200: return r.json()
        except Exception:
            logger.exception(f"Error fetching ABS progress for item {item_id}")
            pass
        return None

    def update_ebook_progress(self, item_id, progress, location):
        """
        Update ebook progress for an item.

        Args:
            item_id: The item ID to update
            progress: The ebook progress as a float (0.0 to 1.0)
            location: Required ebook location (EPUB CFI format)
        """
        # Validate required parameters
        if location is None:
            logger.error("Ebook location is required for progress updates")
            return False

        # Ensure we use a float for the progress
        progress = float(progress)
        url = f"{self.base_url}/api/me/progress/{item_id}"
        payload = {
            "ebookProgress": progress,
            "ebookLocation": location
        }

        try:
            r = self.session.patch(url, json=payload, timeout=self.timeout)
            if r.status_code in (200, 204):
                logger.debug(f"ABS ebook progress updated: {item_id} -> {progress} at location: {location[:50]}...")
                return True
            else:
                logger.error(f"ABS ebook update failed: {r.status_code} - {r.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to update ABS ebook progress: {e}")
            return False

    def update_progress(self, abs_id, timestamp, time_listened):
        """
        Update progress using session-based sync.
        Creates a session, syncs progress, then closes the session.
        """
        if timestamp > 1000000:
            timestamp = timestamp / 1000.0
            logger.warning(f"⚠️ Converted ABS timestamp from milliseconds to seconds: {timestamp}")

        timestamp = float(timestamp)
        if time_listened is None:
            time_listened = 0.0
        time_listened = float(time_listened)

        payload = {
            "currentTime": timestamp,
            "timeListened": time_listened
        }
        return self.update_progress_using_payload(abs_id, payload)

    def update_progress_using_payload(self, abs_id, payload: dict):
        session_id = self.create_session(abs_id)
        if not session_id:
            logger.error(f"Failed to create ABS session for item {abs_id}")
            return {"success": False, "code": None, "reason": f"Failed to create ABS session for item {abs_id}"}

        try:
            url = f"{self.base_url}/api/session/{session_id}/sync"
            r = self.session.post(url, json=payload, timeout=self.timeout)
            if r.status_code in (200, 204):
                logger.debug(f"ABS progress updated via session: {abs_id}, payload: {payload}")
                self.close_session(session_id)
                return {"success": True, "code": r.status_code, "response": r.text}
            elif r.status_code == 404:
                logger.warning(f"ABS session not found (404): {session_id}")
                return {"success": False, "code": 404, "response": r.text}
            else:
                logger.error(f"ABS session sync failed: {r.status_code} - {r.text}")
                return {"success": False, "code": r.status_code, "response": r.text}
        except Exception as e:
            logger.error(f"Failed to sync ABS session progress: {e}")
            return {"success": False, "code": None, "reason": str(e)}

    def get_all_progress_raw(self):
        """Fetch all user progress in one API call."""
        # Try specific progress endpoint first
        url = f"{self.base_url}/api/me/progress"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code == 200:
                data = r.json()
                items = data if isinstance(data, list) else data.get('libraryItemsInProgress', [])
                mapped_items = {item.get('libraryItemId'): item for item in items if item.get('libraryItemId')}
                # logger.debug(f"📊 ABS Bulk Progress (Direct): {len(mapped_items)} items")
                return mapped_items
            elif r.status_code == 404:
                # Fallback to /api/me
                logger.debug("⚠️ /api/me/progress not found (404), falling back to /api/me")
                url_fallback = f"{self.base_url}/api/me"
                r2 = self.session.get(url_fallback, timeout=self.timeout)
                if r2.status_code == 200:
                    data = r2.json()
                    
                    # Try 'mediaInProgress' (some versions) or 'mediaProgress' (others)
                    items = data.get('mediaInProgress', [])
                    if not items:
                        items = data.get('mediaProgress', [])
                        
                    return {item.get('libraryItemId'): item for item in items if item.get('libraryItemId')}
                else:
                    logger.warning(f"⚠️ Fallback to /api/me failed: {r2.status_code}")
            else:
                logger.warning(f"⚠️ Failed to fetch all progress: {r.status_code}")
                
            return {}
        except Exception as e:
            logger.error(f"Error fetching all ABS progress: {e}")
            return {}
        except Exception as e:
            logger.error(f"Error fetching all ABS progress: {e}")
            return {}

    def get_in_progress(self, min_progress=0.01):
        """Fetch in-progress items, optimized to avoid redundant detail fetches if possible."""
        url = f"{self.base_url}/api/me/progress"
        try:
            r = self.session.get(url, timeout=self.timeout)
            if r.status_code != 200: return []
            data = r.json()
            items = data if isinstance(data, list) else data.get('libraryItemsInProgress', [])
            active_items = []
            for item in items:
                # Filter for audiobooks only
                if item.get('mediaType') and item.get('mediaType') != 'audiobook': continue

                duration = item.get('duration', 0)
                current_time = item.get('currentTime', 0)
                if duration == 0 or item.get('isFinished'): continue

                pct = current_time / duration
                if pct >= min_progress:
                    lib_item_id = item.get('libraryItemId') or item.get('itemId')
                    if not lib_item_id: continue

                    # Return basic info without recursive detail fetch if possible
                    # but if we need title/author we might still need it unless we have it in the list
                    title = item.get('metadata', {}).get('title') or "Unknown"
                    author = item.get('metadata', {}).get('authorName')

                    active_items.append({
                        "id": lib_item_id,
                        "title": title,
                        "author": author,
                        "progress": pct,
                        "duration": duration,
                        "source": "ABS",
                        "currentTime": current_time
                    })
            return active_items
        except Exception as e:
            logger.error(f"Error fetching ABS in-progress: {e}")
            return []

    def create_session(self, abs_id):
        """Create a new ABS session for the given abs_id (item id). Returns session_id or None."""
        play_url = f"{self.base_url}/api/items/{abs_id}/play"
        play_payload = {
            "deviceInfo": {
                "id": "abs-kosync-bot",
                "deviceId": "abs-kosync-bot",
                "clientName": "ABS-KoSync-Bridge",
                "clientVersion": "1.0",
                "manufacturer": "ABS-KoSync",
                "model": "Bridge",
                "sdkVersion": "1.0"
            },
            "mediaPlayer": "ABS-KoSync-Bridge",
            "supportedMimeTypes": ["audio/mpeg", "audio/mp4"],
            "forceDirectPlay": True,
            "forceTranscode": False
        }
        try:
            r = self.session.post(play_url, json=play_payload, timeout=self.timeout)
            if r.status_code == 200:
                id = r.json().get('id')
                logger.debug(f"Created new ABS session for item {abs_id}, id: {id}")
                return id
            else:
                logger.error(f"Failed to create ABS session: {r.status_code} - {r.text}")
        except Exception as e:
            logger.error(f"Exception creating ABS session: {e}")
        return None

    def close_session(self, session_id):
        try:
            close_url = f"{self.base_url}/api/session/{session_id}/close"
            self.session.post(close_url, timeout=5)
        except Exception as e:
            logger.warning(f"⚠️ Failed to close session for ABS: {e}")

    def add_to_collection(self, item_id, collection_name=None):
        """Add an audiobook to a collection, creating the collection if it doesn't exist."""
        if not collection_name:
             collection_name = os.environ.get("ABS_COLLECTION_NAME", "abs-kosync")
             
        try:
            collections_url = f"{self.base_url}/api/collections"
            r = self.session.get(collections_url)
            if r.status_code != 200:
                return False

            collections = r.json().get('collections', [])
            target_collection = next((c for c in collections if c.get('name') == collection_name), None)

            if not target_collection:
                lib_url = f"{self.base_url}/api/libraries"
                r_lib = self.session.get(lib_url)
                if r_lib.status_code == 200:
                    libraries = r_lib.json().get('libraries', [])
                    if libraries:
                        r_create = self.session.post(collections_url,
                                                 json={"libraryId": libraries[0]['id'], "name": collection_name})
                        if r_create.status_code in [200, 201]:
                            target_collection = r_create.json()

            if not target_collection:
                return False

            add_url = f"{self.base_url}/api/collections/{target_collection['id']}/book"
            r_add = self.session.post(add_url, json={"id": item_id})
            if r_add.status_code in [200, 201, 204]:
                try:
                    details = self.get_item_details(item_id)
                    title = details.get('media', {}).get('metadata', {}).get('title') if details else None
                except Exception:
                    title = None
                logger.info(f"🏷️ Added '{sanitize_log_data(title or str(item_id))}' to ABS Collection: {collection_name}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error adding item to ABS collection: {e}")
            return False

    def remove_from_collection(self, item_id, collection_name="abs-kosync"):
        """Remove an audiobook from a collection."""
        try:
            # Get collection by name
            collections_url = f"{self.base_url}/api/collections"
            r = self.session.get(collections_url)
            if r.status_code != 200:
                logger.warning(f"Failed to fetch collections to remove item {item_id}")
                return False

            collections = r.json().get('collections', [])
            target_collection = next((c for c in collections if c.get('name') == collection_name), None)

            if not target_collection:
                logger.warning(f"Collection '{collection_name}' not found, cannot remove item {item_id}")
                return False

            # Remove from collection
            remove_url = f"{self.base_url}/api/collections/{target_collection['id']}/book/{item_id}"
            r_remove = self.session.delete(remove_url)
            
            if r_remove.status_code in [200, 201, 204]:
                logger.info(f"🗑️ Removed item {item_id} from ABS Collection: {collection_name}")
                return True
            else:
                logger.warning(f"Failed to remove item {item_id} from collection {collection_name}: {r_remove.status_code} - {r_remove.text}")
                return False

        except Exception as e:
            logger.error(f"Error removing item from ABS collection: {e}")
            return False

class KoSyncClient:
    def __init__(self):
        self.base_url = os.environ.get("KOSYNC_SERVER", "").rstrip('/')
        self.user = os.environ.get("KOSYNC_USER")
        self.auth_token = hash_kosync_key(os.environ.get("KOSYNC_KEY", ""))
        self.session = requests.Session()

    def is_configured(self):
        enabled_val = os.environ.get("KOSYNC_ENABLED", "").lower()
        if enabled_val == 'false':
            return False
        return bool(self.base_url and self.user)

    def check_connection(self):
        if not self.is_configured():
            logger.warning("⚠️ KoSync not configured (skipping)")
            return False
            
        is_local = '127.0.0.1' in self.base_url or 'localhost' in self.base_url
        url = f"{self.base_url}/healthcheck"
        headers = kosync_auth_headers(self.user, self.auth_token)
        try:
            r = self.session.get(url, timeout=5, headers=headers)
            if r.status_code == 200:
                # First-run visible INFO, otherwise DEBUG
                first_run_marker = '/data/.first_run_done'
                try:
                    first_run = not os.path.exists(first_run_marker)
                except Exception:
                    first_run = False

                if first_run:
                    logger.info(f"✅ Connected to KoSync Server at {self.base_url}")
                    try:
                        open(first_run_marker, 'w').close()
                    except Exception:
                        pass
                return True
            # Fallback check
            url_sync = f"{self.base_url}/syncs/progress/test-connection"
            r = self.session.get(url_sync, headers=headers, timeout=5)
            if r.status_code == 200:
                return True
            logger.warning(f"❌ KoSync connection failed (Response: {r.status_code})")
            return False
        except Exception as e:
            if is_local:
                # Expected race condition during startup
                logger.debug(f"ℹ️  KoSync (Internal): Server check skipped during startup (will be ready shortly)")
                return True
            logger.warning(f"❌ KoSync Error: {e}")
            return False

    def get_progress(self, doc_id):
        """
        CRITICAL FIX: Returns TUPLE (percentage, xpath_string)
        This prevents the 'cannot unpack non-iterable float' crash.
        """
        headers = kosync_auth_headers(self.user, self.auth_token)
        url = f"{self.base_url}/syncs/progress/{doc_id}"
        try:
            r = self.session.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                pct = float(data.get('percentage', 0))
                # Grab the raw progress string (XPath)
                xpath = data.get('progress')
                return pct, xpath
        except Exception as e:
            logger.error(f"Error fetching KoSync progress for doc {doc_id}: {e}")
            pass
        return None, None

    def update_progress(self, doc_id, percentage, xpath=None):
        if not self.is_configured(): return False

        headers = {
            **kosync_auth_headers(self.user, self.auth_token),
            "content-type": "application/json",
        }
        url = f"{self.base_url}/syncs/progress"

        # Use XPath if provided, otherwise format percentage
        progress_val = xpath if xpath else ""

        payload = {
            "document": doc_id,
            "percentage": percentage,
            "progress": progress_val,
            "device": "abs-sync-bot",
            "device_id": "abs-sync-bot",
            "timestamp": int(time.time())
        }
        try:
            r = self.session.put(url, headers=headers, json=payload, timeout=10)
            if r.status_code in (200, 201, 204):
                logger.debug(f"   📡 KoSync Updated: {percentage:.1%} with progress '{progress_val}' for doc {doc_id}")
                return True
            else:
                logger.error(f"Failed to update KoSync: {r.status_code} - {r.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to update KoSync: {e}")
            return False
# [END FILE]
