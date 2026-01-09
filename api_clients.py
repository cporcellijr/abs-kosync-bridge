# [START FILE: abs-kosync-enhanced/api_clients.py]
import os
import requests
import logging
import time
import hashlib

from logging_utils import sanitize_log_data

logger = logging.getLogger(__name__)

class ABSClient:
    def __init__(self):
        # Kept your variable names (ABS_SERVER / ABS_KEY)
        self.base_url = os.environ.get("ABS_SERVER", "").rstrip('/')
        self.token = os.environ.get("ABS_KEY")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def check_connection(self):
        # Verify configuration first
        if not self.base_url or not self.token:
            logger.warning("⚠️ Audiobookshelf not configured (skipping)")
            return False

        url = f"{self.base_url}/api/me"
        try:
            r = requests.get(url, headers=self.headers, timeout=5)
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
            r = requests.get(lib_url, headers=self.headers)
            if r.status_code != 200: return []
            libraries = r.json().get('libraries', [])
            all_audiobooks = []
            for lib in libraries:
                items_url = f"{self.base_url}/api/libraries/{lib['id']}/items"
                params = {"mediaType": "audiobook"}
                r_items = requests.get(items_url, headers=self.headers, params=params)
                if r_items.status_code == 200:
                    all_audiobooks.extend(r_items.json().get('results', []))
            return all_audiobooks
        except Exception as e:
            logger.error(f"Exception fetching audiobooks: {e}")
            return []

    def get_audio_files(self, item_id):
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
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

    def get_item_details(self, item_id):
        url = f"{self.base_url}/api/items/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200: return r.json()
        except: pass
        return None

    def get_progress(self, item_id):
        url = f"{self.base_url}/api/me/progress/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200: return r.json().get('currentTime', 0)
        except: pass
        return 0.0

    def update_progress(self, item_id, timestamp):
        # Sanity check: if timestamp looks like milliseconds (greater than 1,000,000), convert to seconds
        if timestamp > 1000000:
            timestamp = timestamp / 1000.0
            logger.warning(f"⚠️ Converted ABS timestamp from milliseconds to seconds: {timestamp}")
        # Ensure we use a float for the payload
        timestamp = float(timestamp)
        url = f"{self.base_url}/api/me/progress/{item_id}"
        payload = {"currentTime": timestamp, "duration": 0, "isFinished": False}
        try:
            r = requests.patch(url, headers=self.headers, json=payload, timeout=10)
            if r.status_code in (200, 204):
                logger.debug(f"ABS progress updated: {item_id} -> {timestamp}")
                return True
            else:
                logger.error(f"ABS update failed: {r.status_code} - {r.text}")
                return False
        except Exception as e:
            logger.error(f"Failed to update ABS progress: {e}")
            return False

    def get_in_progress(self, min_progress=0.01):
        url = f"{self.base_url}/api/me/progress"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            if r.status_code != 200: return []
            data = r.json()
            # Handle both direct list and wrapped dictionary response formats
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

                    # Quick detail fetch to get Title/Author
                    details = self.get_item_details(lib_item_id)
                    if not details: continue
                    metadata = details.get('media', {}).get('metadata', {})

                    active_items.append({
                        "id": lib_item_id,
                        "title": metadata.get('title', details.get('name', 'Unknown')),
                        "author": metadata.get('authorName'),
                        "progress": pct,
                        "duration": duration,
                        "source": "ABS"
                    })
            return active_items
        except Exception as e:
            logger.error(f"Error fetching ABS sessions: {e}")
            return []

class KoSyncClient:
    def __init__(self):
        self.base_url = os.environ.get("KOSYNC_SERVER", "").rstrip('/')
        self.user = os.environ.get("KOSYNC_USER")
        # Kept your MD5 hash logic
        self.auth_token = hashlib.md5(os.environ.get("KOSYNC_KEY", "").encode('utf-8')).hexdigest()

    def is_configured(self):
        return bool(self.base_url and self.user)

    def check_connection(self):
        if not self.is_configured():
            logger.warning("⚠️ KoSync not configured (skipping)")
            return False
        url = f"{self.base_url}/healthcheck"
        try:
            headers = {'accept': 'application/vnd.koreader.v1+json'}
            r = requests.get(url, timeout=5, headers = headers)
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
            headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token}
            r = requests.get(url_sync, headers=headers, timeout=5)
            if r.status_code == 200:
                first_run_marker = '/data/.first_run_done'
                try:
                    first_run = not os.path.exists(first_run_marker)
                except Exception:
                    first_run = False

                if first_run:
                    logger.info(f"✅ Connected to KoSync Server (Response: {r.status_code})")
                    try:
                        open(first_run_marker, 'w').close()
                    except Exception:
                        pass
                return True
            logger.warning(f"❌ KoSync connection failed (Response: {r.status_code})")
            return False
        except Exception as e:
            logger.warning(f"❌ KoSync Error: {e}")
            return False

    def get_progress(self, doc_id):
        """
        CRITICAL FIX: Returns TUPLE (percentage, xpath_string)
        This prevents the 'cannot unpack non-iterable float' crash.
        """
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token, 'accept': 'application/vnd.koreader.v1+json'}
        url = f"{self.base_url}/syncs/progress/{doc_id}"
        try:
            r = requests.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                logger.debug(f"KoSync get_progress data: {data}")
                pct = float(data.get('percentage', 0))
                # Grab the raw progress string (XPath)
                xpath = data.get('progress')
                return pct, xpath
        except: pass
        return 0.0, None

    def update_progress(self, doc_id, percentage, xpath=None):
        if not self.is_configured(): return

        headers = {
            "x-auth-user": self.user,
            "x-auth-key": self.auth_token,
            'accept': 'application/vnd.koreader.v1+json',
            'content-type': 'application/json'
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
            r = requests.put(url, headers=headers, json=payload, timeout=10)
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