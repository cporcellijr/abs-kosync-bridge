"""
Storyteller DB Integration for abs-kosync-bridge

HARDENED VERSION with:
- SQLite WAL mode for better concurrent access
- Smart leapfrog: max(now, storyteller_ts) + 1 second
- Session timestamp coordination
"""

import sqlite3
import logging
import os
import json
import time
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class StorytellerDB:
    """
    Storyteller SQLite database integration.
    
    SCHEMA:
    - book: uuid, title
    - position: uuid, book_uuid, user_id, locator (JSON), timestamp (ms), updated_at
    - session: user_id, updated_at
    
    LEAPFROG STRATEGY:
    Mobile apps cache reading positions and may push stale data.
    We set timestamp to max(now, current_ts) + 1 second to always win.
    """
    
    def __init__(self, db_path=None):
        if db_path is None:
            db_path = os.getenv("STORYTELLER_DB_PATH", "/data/storyteller.db")
        self.db_path = Path(db_path)
        self.user_id = os.getenv("STORYTELLER_USER_ID")
        self.min_leapfrog_ms = int(os.getenv("STORYTELLER_LEAPFROG_MS", 1000))
        
        # Enable WAL mode on init
        self._enable_wal_mode()
        
        logger.info(f"StorytellerDB: {self.db_path} (WAL mode, leapfrog={self.min_leapfrog_ms}ms)")

    def _enable_wal_mode(self):
        """Enable SQLite WAL mode for better concurrent access."""
        if not self.db_path.exists():
            logger.warning(f"Storyteller DB not found: {self.db_path}")
            return
        
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=15.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            conn.close()
            logger.debug(f"SQLite mode: {mode}")
        except Exception as e:
            logger.warning(f"Could not enable WAL mode: {e}")

    @contextmanager
    def _get_connection(self):
        """Get DB connection with WAL mode and timeout."""
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def check_connection(self):
        """Test database connectivity."""
        if not self.db_path.exists():
            logger.error(f"❌ Storyteller DB not found: {self.db_path}")
            return False
        
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                mode = cursor.execute("PRAGMA journal_mode").fetchone()[0]
                pos_count = cursor.execute("SELECT COUNT(*) FROM position").fetchone()[0]
                book_count = cursor.execute("SELECT COUNT(*) FROM book").fetchone()[0]
                logger.info(f"✅ Storyteller DB: {book_count} books, {pos_count} positions (mode={mode})")
                return True
        except Exception as e:
            logger.error(f"❌ Storyteller DB error: {e}")
            return False

    def _compute_leapfrog_timestamp(self, current_ts_ms):
        """
        Compute timestamp that beats Storyteller's current value.
        
        Formula: max(now, current_ts) + leapfrog_ms
        
        This ensures our update always wins over cached values.
        """
        now_ms = time.time() * 1000
        base_ts = max(now_ms, current_ts_ms or 0)
        leapfrog_ts = base_ts + self.min_leapfrog_ms
        
        # Convert to string format for updated_at field
        leapfrog_dt = datetime.fromtimestamp(leapfrog_ts / 1000, tz=timezone.utc)
        updated_at_str = leapfrog_dt.strftime('%Y-%m-%d %H:%M:%S')
        
        return leapfrog_ts, updated_at_str

    def _find_book_uuid(self, conn, ebook_filename):
        """Find book UUID by matching filename to title."""
        cursor = conn.cursor()
        cursor.execute("SELECT uuid, title FROM book")
        results = cursor.fetchall()
        
        # Clean filename for matching
        clean_filename = Path(ebook_filename).stem.lower()
        clean_filename = clean_filename.replace("(readaloud)", "").strip()
        
        for row in results:
            book_title = row['title'].lower()
            if book_title in clean_filename or clean_filename in book_title:
                return row['uuid'], row['title']
        
        return None, None

    def _update_session(self, conn, user_id, updated_at_str):
        """Update session timestamp to match position update."""
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE session SET updated_at = ? WHERE user_id = ?",
                (updated_at_str, user_id)
            )
            logger.debug(f"Updated {cursor.rowcount} session(s)")
        except Exception as e:
            logger.warning(f"Session update failed: {e}")

    def update_progress(self, ebook_filename, percentage, source_timestamp=None):
        """
        Update reading progress with smart leapfrog timestamp.
        
        Args:
            ebook_filename: The EPUB filename
            percentage: Progress as decimal (0.0 to 1.0)
            source_timestamp: Ignored, kept for API compatibility
        
        Returns:
            True if successful, False otherwise
        """
        if not self.db_path.exists():
            return False
        
        try:
            with self._get_connection() as conn:
                book_uuid, book_title = self._find_book_uuid(conn, ebook_filename)
                if not book_uuid:
                    logger.warning(f"Book not found in Storyteller: {ebook_filename}")
                    return False
                
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT uuid, user_id, locator, timestamp FROM position WHERE book_uuid = ?",
                    (book_uuid,)
                )
                rows = cursor.fetchall()
                
                if not rows:
                    logger.warning(f"No position entries for: {book_title}")
                    return False
                
                updated_count = 0
                for row in rows:
                    pos_uuid = row['uuid']
                    user_id = row['user_id']
                    current_ts = float(row['timestamp']) if row['timestamp'] else 0
                    
                    # Compute leapfrog timestamp
                    new_ts, updated_at_str = self._compute_leapfrog_timestamp(current_ts)
                    
                    # Parse and update locator JSON
                    try:
                        locator = json.loads(row['locator']) if row['locator'] else {}
                    except json.JSONDecodeError:
                        locator = {}
                    
                    if 'locations' not in locator:
                        locator['locations'] = {}
                    locator['locations']['totalProgression'] = float(percentage)
                    
                    # Update position
                    cursor.execute(
                        "UPDATE position SET locator = ?, timestamp = ?, updated_at = ? WHERE uuid = ?",
                        (json.dumps(locator), new_ts, updated_at_str, pos_uuid)
                    )
                    
                    # Update session
                    self._update_session(conn, user_id, updated_at_str)
                    updated_count += 1
                
                delta_s = (new_ts - current_ts) / 1000 if current_ts else 0
                logger.info(f"✅ Storyteller: {book_title} → {percentage:.1%} (ts+{delta_s:.1f}s)")
                return True
                
        except Exception as e:
            logger.error(f"Storyteller write error: {e}")
            return False

    def get_progress(self, ebook_filename):
        """
        Get reading progress.
        
        Returns: (percentage, timestamp_seconds) or (None, 0)
        """
        result = self.get_progress_with_fragment(ebook_filename)
        return result[0], result[1]

    def get_progress_with_fragment(self, ebook_filename):
        """
        Get detailed progress including fragment info.
        
        Returns: (percentage, timestamp_seconds, href, fragment_id)
        """
        if not self.db_path.exists():
            return None, 0, None, None
        
        try:
            with self._get_connection() as conn:
                book_uuid, _ = self._find_book_uuid(conn, ebook_filename)
                if not book_uuid:
                    return None, 0, None, None
                
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT locator, timestamp FROM position 
                    WHERE book_uuid = ? 
                    ORDER BY timestamp DESC LIMIT 1
                    """,
                    (book_uuid,)
                )
                
                row = cursor.fetchone()
                if row:
                    try:
                        locator = json.loads(row['locator']) if row['locator'] else {}
                    except json.JSONDecodeError:
                        locator = {}
                    
                    pct = float(locator.get('locations', {}).get('totalProgression', 0.0))
                    ts = float(row['timestamp']) / 1000.0 if row['timestamp'] else 0.0
                    href = locator.get('href')
                    fragments = locator.get('locations', {}).get('fragments', [])
                    fragment_id = fragments[0] if fragments else None
                    
                    return pct, ts, href, fragment_id
                    
        except Exception as e:
            logger.error(f"Storyteller read error: {e}")
        
        return None, 0, None, None
