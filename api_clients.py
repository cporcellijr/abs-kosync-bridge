# [START FILE: abs-kosync-enhanced/api_clients.py]
import os
import requests
import logging
import time
import hashlib

logger = logging.getLogger(__name__)

class ABSClient:
    def __init__(self):
        self.base_url = os.environ.get("ABS_SERVER", "").rstrip('/')
        self.token = os.environ.get("ABS_KEY")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def check_connection(self):
        url = f"{self.base_url}/api/me"
        try:
            r = requests.get(url, headers=self.headers, timeout=5)
            if r.status_code == 200:
                logger.info(f"✅ Connected to Audiobookshelf as user: {r.json().get('username', 'Unknown')}")
                return True
            else:
                logger.error(f"❌ Audiobookshelf Connection Failed: {r.status_code} - {r.text}")
                return False
        except requests.exceptions.ConnectionError:
            logger.error(f"❌ Could not connect to Audiobookshelf at {self.base_url}. Check URL and Docker Network.")
            return False
        except Exception as e:
            logger.error(f"❌ Audiobookshelf Error: {e}")
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
                for af in data.get('media', {}).get('audioFiles', []):
                    ext = af.get('metadata', {}).get('ext') or 'mp3'
                    if not ext.startswith('.'): ext = f".{ext}"
                    stream_url = f"{self.base_url}/api/items/{item_id}/file/{af['ino']}?token={self.token}"
                    files.append({"stream_url": stream_url, "ext": ext})
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
        url = f"{self.base_url}/api/me/progress/{item_id}"
        payload = {"currentTime": timestamp, "duration": 0, "isFinished": False}
        try: requests.patch(url, headers=self.headers, json=payload)
        except Exception as e: logger.error(f"Failed to update ABS progress: {e}")

    def get_in_progress(self, min_progress=0.01):
        url = f"{self.base_url}/api/me/progress"
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            if r.status_code != 200: return []
            data = r.json()
            items = data if isinstance(data, list) else data.get('libraryItemsInProgress', [])
            active_items = []
            for item in items:
                if item.get('mediaType') and item.get('mediaType') != 'audiobook': continue
                duration = item.get('duration', 0)
                current_time = item.get('currentTime', 0)
                if duration == 0 or item.get('isFinished'): continue
                pct = current_time / duration
                if pct >= min_progress:
                    lib_item_id = item.get('libraryItemId') or item.get('itemId')
                    if not lib_item_id: continue
                    details = self.get_item_details(lib_item_id)
                    if not details: continue
                    metadata = details.get('media', {}).get('metadata', {})
                    active_items.append({
                        "id": lib_item_id,
                        "title": metadata.get('title', details.get('name', 'Unknown')),
                        "author": metadata.get('authorName'),
                        "progress": pct,
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
        self.auth_token = hashlib.md5(os.environ.get("KOSYNC_KEY", "").encode('utf-8')).hexdigest()

    def is_configured(self):
        """Return True if KoSync is configured, False otherwise."""
        return bool(self.base_url and self.user)

    def check_connection(self):
        url = f"{self.base_url}/healthcheck"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                 logger.info(f"✅ Connected to KoSync Server at {self.base_url}")
                 return True
            url_sync = f"{self.base_url}/syncs/progress/test-connection"
            headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token}
            r = requests.get(url_sync, headers=headers, timeout=5)
            logger.info(f"✅ Connected to KoSync Server (Response: {r.status_code})")
            return True
        except Exception as e:
            logger.error(f"❌ KoSync Error: {e}")
            return False

    def get_progress(self, doc_id):
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token, 'accept': 'application/vnd.koreader.v1+json'}
        url = f"{self.base_url}/syncs/progress/{doc_id}"
        try:
            r = requests.get(url, headers=headers)
            if r.status_code == 200: return float(r.json().get('percentage', 0))
        except: pass
        return 0.0

    def update_progress(self, doc_id, percentage, xpath=None):
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token, 'accept': 'application/vnd.koreader.v1+json', 'content-type': 'application/json'}
        url = f"{self.base_url}/syncs/progress"
        progress_val = xpath if xpath else f"{percentage:.2%}"
        payload = {
            "document": doc_id, "percentage": percentage, "progress": progress_val, 
            "device": "abs-sync-bot", "device_id": "abs-sync-bot", "timestamp": int(time.time())
        }
        try:
            requests.put(url, headers=headers, json=payload)
            logger.info(f"KoSync updated successfully")
        except Exception as e:
            logger.error(f"Failed to update KoSync: {e}")
# [END FILE]