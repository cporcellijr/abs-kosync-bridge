# [START FILE: abs-kosync-enhanced/storyteller_db.py]
import sqlite3
import os
import logging
import time
import json
from pathlib import Path

from src.sync_clients.sync_client_interface import LocatorResult

logger = logging.getLogger(__name__)

class StorytellerDB:
    def __init__(self):
        self.db_path = Path(os.environ.get("STORYTELLER_DB_PATH", "/storyteller_data/storyteller.db"))
        self.conn = None
        self.connection_succeeded = self._init_connection()

    def is_configured(self):
        return self.connection_succeeded

    def _init_connection(self):
        if not self.db_path.exists(): return False
        try:
            self.conn = sqlite3.connect(f"file:{self.db_path}", uri=True, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL;")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Storyteller DB: {e}")
            return False

    def check_connection(self):
        if not self.conn: return self._init_connection()
        try:
            self.conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error as e:
            logger.debug(f"Storyteller DB connection check failed: {e}")
            return False

    def get_progress(self, ebook_filename):
        if not self.conn: return None, None
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT uuid FROM book WHERE title LIKE ?", (f"%{Path(ebook_filename).stem}%",))
            row = cursor.fetchone()
            if not row: return None, None
            cursor.execute("SELECT locator, timestamp FROM position WHERE book_uuid = ? ORDER BY timestamp DESC LIMIT 1", (row['uuid'],))
            pos = cursor.fetchone()
            if pos and pos['locator']:
                data = json.loads(pos['locator'])
                return float(data.get('locations', {}).get('totalProgression', 0)), pos['timestamp']
            return None, None
        except (sqlite3.Error, json.JSONDecodeError, KeyError) as e:
            logger.debug(f"Storyteller get_progress failed: {e}")
            return None, None

    def get_progress_with_fragment(self, ebook_filename):
        if not self.conn: return None, None, None, None
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT uuid FROM book WHERE title LIKE ?", (f"%{Path(ebook_filename).stem}%",))
            row = cursor.fetchone()
            if not row: return None, None, None, None
            cursor.execute("SELECT locator, timestamp FROM position WHERE book_uuid = ? ORDER BY timestamp DESC LIMIT 1", (row['uuid'],))
            pos = cursor.fetchone()
            if pos and pos['locator']:
                data = json.loads(pos['locator'])
                href = data.get('href', '')
                frag = href.split('#')[1] if '#' in href else None
                return float(data.get('locations', {}).get('totalProgression', 0)), pos['timestamp'], href.split('#')[0], frag
            return None, None, None, None
        except (sqlite3.Error, json.JSONDecodeError, KeyError) as e:
            logger.debug(f"Storyteller get_progress_with_fragment failed: {e}")
            return None, None, None, None

    def update_progress(self, ebook_filename, percentage, rich_locator: LocatorResult = None):
        if not self.db_path.exists():
            return False  # Silently skip if Storyteller DB not available

        try:
            with sqlite3.connect(f"file:{self.db_path}", uri=True, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT uuid FROM book WHERE title LIKE ?", (f"%{Path(ebook_filename).stem}%",))
                row = cursor.fetchone()
                if not row: return False
                uuid = row['uuid']

                cursor.execute("SELECT locator, timestamp FROM position WHERE book_uuid = ? ORDER BY timestamp DESC LIMIT 1", (uuid,))
                pos_row = cursor.fetchone()

                # 10s leapfrog logic
                current_ts = pos_row['timestamp'] if pos_row else 0
                new_ts = max(int(time.time() * 1000), current_ts)

                locator = {}
                if pos_row and pos_row['locator']:
                    try: locator = json.loads(pos_row['locator'])
                    except json.JSONDecodeError: pass

                if 'locations' not in locator: locator['locations'] = {}
                locator['locations']['totalProgression'] = float(percentage)

                # Apply Rich Locator if available
                if rich_locator:
                    if rich_locator.href: locator['href'] = rich_locator.href
                    if rich_locator.css_selector: locator['locations']['cssSelector'] = rich_locator.css_selector

                # Clear conflicting fields if we are just setting percentage
                elif not rich_locator:
                    for k in ['cssSelector', 'fragments', 'position', 'progression']:
                        if k in locator['locations']: del locator['locations'][k]

                cursor.execute("UPDATE position SET locator = ?, timestamp = ? WHERE uuid = ?", (json.dumps(locator), new_ts, uuid))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"Storyteller write error: {e}")
            return False

    def get_recent_activity(self, hours=24, min_progress=0.01):
        if not self.conn: return []
        cutoff = int((time.time() - hours*3600)*1000)
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT b.uuid, b.title, p.locator FROM book b JOIN position p ON b.uuid = p.book_uuid WHERE p.timestamp > ?", (cutoff,))
            results = []
            seen = set()
            for row in cursor.fetchall():
                if row['uuid'] in seen: continue
                try:
                    pct = json.loads(row['locator']).get('locations', {}).get('totalProgression', 0)
                    if pct > min_progress:
                        results.append({"id": row['uuid'], "title": row['title'], "source": "STORYTELLER"})
                        seen.add(row['uuid'])
                except (json.JSONDecodeError, KeyError): pass
            return results
        except sqlite3.Error as e:
            logger.warning(f"Storyteller get_recent_activity query failed: {e}")
            return []

    def add_to_collection(self, ebook_filename): pass

    def get_book_uuid(self, ebook_filename):
        if not self.conn: return None
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT uuid FROM book WHERE title LIKE ?", (f"%{Path(ebook_filename).stem}%",))
            row = cursor.fetchone()
            return row['uuid'] if row else None
        except sqlite3.Error as e:
            logger.debug(f"Storyteller get_book_uuid failed: {e}")
            return None

    def force_position_update(self, ebook_filename, percentage, target_href=None):
        try:
            with sqlite3.connect(f"file:{self.db_path}", uri=True, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT uuid FROM book WHERE title LIKE ?", (f"%{Path(ebook_filename).stem}%",))
                row = cursor.fetchone()
                if not row: return False
                uuid = row['uuid']

                new_ts = int(time.time() * 1000)
                locator = {
                    "type": "application/xhtml+xml",
                    "locations": {"totalProgression": float(percentage)}
                }
                if target_href: locator["href"] = target_href

                cursor.execute("UPDATE position SET locator = ?, timestamp = ? WHERE uuid = ?", (json.dumps(locator), new_ts, uuid))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.warning(f"Storyteller force_position_update failed: {e}")
            return False
# [END FILE]