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
            if r.status_code != 200:
                logger.error(f"Failed to fetch libraries: {r.status_code} - {r.text}")
                return []
            
            libraries = r.json().get('libraries', [])
            all_audiobooks = []

            for lib in libraries:
                logger.info(f"Scanning library: {lib['name']}...")
                lib_id = lib['id']
                items_url = f"{self.base_url}/api/libraries/{lib_id}/items"
                params = {"mediaType": "audiobook"}
                r_items = requests.get(items_url, headers=self.headers, params=params)
                if r_items.status_code == 200:
                    results = r_items.json().get('results', [])
                    all_audiobooks.extend(results)
                else:
                    logger.warning(f"Could not fetch items for library {lib['name']}")

            logger.info(f"Found {len(all_audiobooks)} audiobooks across {len(libraries)} libraries.")
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
                audio_files = data.get('media', {}).get('audioFiles', [])
                for af in audio_files:
                    stream_url = f"{self.base_url}/api/items/{item_id}/file/{af['ino']}"
                    stream_url += f"?token={self.token}" 
                    
                    extension = af.get('metadata', {}).get('ext') or 'mp3'
                    if not extension.startswith('.'):
                        extension = f".{extension}"
                    
                    files.append({
                        "stream_url": stream_url,
                        "ext": extension
                     })
                return files
            else:
                logger.error(f"Failed to get audio files for {item_id}: {r.status_code} - {r.text}")
                return []
        except Exception as e:
            logger.error(f"Error getting audio files: {e}")
            return []

    def get_progress(self, item_id):
        url = f"{self.base_url}/api/me/progress/{item_id}"
        try:
            r = requests.get(url, headers=self.headers)
            if r.status_code == 200:
                return r.json().get('currentTime', 0)
        except Exception:
            pass
        return 0.0

    def update_progress(self, item_id, timestamp):
        url = f"{self.base_url}/api/me/progress/{item_id}"
        payload = {
            "currentTime": timestamp,
            "duration": 0, 
            "isFinished": False
        }
        try:
            requests.patch(url, headers=self.headers, json=payload)
        except Exception as e:
            logger.error(f"Failed to update ABS progress: {e}")

class KoSyncClient:
    def __init__(self):
        self.base_url = os.environ.get("KOSYNC_SERVER", "").rstrip('/')
        self.user = os.environ.get("KOSYNC_USER")
        self.auth_token = hashlib.md5(os.environ.get("KOSYNC_KEY", "").encode('utf-8')).hexdigest()

    def check_connection(self):
        url = f"{self.base_url}/healthcheck"
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token}
        try:
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                 logger.info(f"✅ Connected to KoSync Server at {self.base_url}")
                 return True
            
            url_sync = f"{self.base_url}/syncs/progress/test-connection"
            r = requests.get(url_sync, headers=headers, timeout=5)
            logger.info(f"✅ Connected to KoSync Server (Response: {r.status_code})")
            return True
        except requests.exceptions.ConnectionError:
            logger.error(f"❌ Could not connect to KoSync at {self.base_url}. Check URL.")
            return False
        except Exception as e:
            logger.error(f"❌ KoSync Error: {e}")
            return False

    def get_progress(self, doc_id):
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token}
        url = f"{self.base_url}/syncs/progress/{doc_id}"
        try:
            r = requests.get(url, headers=headers)
            if r.status_code == 200:
                data = r.json()
                return float(data.get('percentage', 0))
        except Exception:
            pass
        return 0.0

    def update_progress(self, doc_id, percentage, xpath=None):
        headers = {"x-auth-user": self.user, "x-auth-key": self.auth_token}
        url = f"{self.base_url}/syncs/progress"
        
        # Use XPath if generated, otherwise fallback to percentage string
        progress_val = xpath if xpath else f"{percentage:.2%}"
        
        payload = {
            "document": doc_id,
            "percentage": percentage,
            "progress": progress_val, 
            "device": "abs-sync-bot",
            "device_id": "abs-sync-bot", 
            "timestamp": int(time.time())
        }
        
        try:
            # Reverted to simple PUT logic
            r = requests.put(url, headers=headers, json=payload)
            
            if r.status_code not in [200, 201]:
                logger.error(f"KoSync Update Failed: {r.status_code} - {r.text}")
            else:
                logger.info(f"KoSync updated successfully (HTTP {r.status_code})")
                
        except Exception as e:
            logger.error(f"Failed to update KoSync: {e}")
