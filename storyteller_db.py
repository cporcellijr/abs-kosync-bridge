import sqlite3
import logging
import os
import json
import time
import uuid
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class StorytellerDB:
    """
    Storyteller DB Integration for abs-kosync-bridge
    
    CONFIRMED SCHEMA (Dec 2025):
    =============================
    session table:
        - id, user_id, session_token, expires, created_at, updated_at
        
    position table:
        - uuid, user_id, book_uuid, locator, timestamp, created_at, updated_at
        
    Note: Sessions are per-USER, not per-BOOK
    """
    
    def __init__(self, db_path=None, **kwargs):
        if db_path is None:
            db_path = os.getenv("STORYTELLER_DB_PATH", "/data/storyteller.db")
        self.db_path = Path(db_path)
        self.user_id = os.getenv("STORYTELLER_USER_ID")
        logger.info(f"Initialized StorytellerDB at {self.db_path} (User: {self.user_id})")

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def check_connection(self):
        """REQUIRED BY main.py: Validates the DB is accessible."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM position")
                pos_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM session")
                sess_count = cursor.fetchone()[0]
                logger.info(f"✅ Storyteller DB connected ({pos_count} positions, {sess_count} sessions)")
                return True
        except Exception as e:
            logger.error(f"❌ Storyteller DB connection FAILED: {e}")
            return False

    def _get_timestamp_formats(self):
        """
        Returns both timestamp formats used by Storyteller:
        1. timestamp column: 1767016380036.0 (float milliseconds)
        2. updated_at column: "2025-12-29 13:53:00" (string datetime)
        
        Adds 60 second leapfrog to beat Storyteller app's internal cache.
        """
        now = datetime.now(timezone.utc)
        
        # Add 60s leapfrog to ensure we're always "newer" than app cache
        timestamp_ms = float((now.timestamp() + 60) * 1000)
        
        # String format for updated_at columns
        updated_at_str = now.strftime('%Y-%m-%d %H:%M:%S')
        
        return timestamp_ms, updated_at_str

    def _parse_timestamp(self, ts_val):
        """Converts DB timestamp to milliseconds"""
        if not ts_val: 
            return 0
        try:
            val = float(ts_val)
            if val < 10000000000:  # Looks like seconds, convert to ms
                return int(val * 1000)
            return int(val)
        except (ValueError, TypeError):
            try:
                # Try parsing as date string
                dt = datetime.fromisoformat(str(ts_val).replace('Z', '+00:00'))
                return int(dt.timestamp() * 1000)
            except:
                return 0

    def _find_book_uuid(self, conn, ebook_filename):
        """Find book UUID by matching filename to book title"""
        cursor = conn.cursor()
        cursor.execute("SELECT uuid, title FROM book")
        results = cursor.fetchall()
        
        for row in results:
            book_title = row['title']
            # Bidirectional matching (either direction works)
            if (book_title.lower() in ebook_filename.lower() or 
                ebook_filename.lower() in book_title.lower()):
                return row['uuid'], book_title
        
        return None, None

    def _update_session(self, conn, user_id, updated_at_str):
        """
        Update session.updated_at for this user's most recent session.
        
        CRITICAL: Sessions are per-USER, not per-book!
        We update the most recent session regardless of which book is being read.
        """
        try:
            cursor = conn.cursor()
            
            # Update the most recent session for this user
            cursor.execute("""
                UPDATE session 
                SET updated_at = ? 
                WHERE user_id = ? 
                AND id = (
                    SELECT id 
                    FROM session 
                    WHERE user_id = ? 
                    ORDER BY updated_at DESC 
                    LIMIT 1
                )
            """, (updated_at_str, user_id, user_id))
            
            if cursor.rowcount > 0:
                logger.debug(f"  📝 Session updated → {updated_at_str}")
            else:
                logger.debug(f"  ℹ️  No existing session found for user {user_id}")
                
        except Exception as e:
            logger.warning(f"  ⚠️  Session update failed: {e}")

    def update_progress(self, ebook_filename, percentage, source_timestamp=None):
        """
        Pushes ABS/KoSync progress INTO Storyteller DB.
        
        Updates THREE locations with coordinated timestamps:
        1. position.locator (JSON with totalProgression)
        2. position.timestamp (float ms) + position.updated_at (string)
        3. session.updated_at (string) - for the user's most recent session
        
        Returns:
            bool: True if successful, False otherwise
        """
        try:
            with self._get_connection() as conn:
                book_uuid, book_title = self._find_book_uuid(conn, ebook_filename)
                
                if not book_uuid:
                    logger.warning(f"  ⚠️  Book not found in Storyteller DB: {ebook_filename}")
                    return False

                cursor = conn.cursor()
                
                # Get all position entries for this book
                cursor.execute("""
                    SELECT user_id, locator, timestamp, updated_at, uuid 
                    FROM position 
                    WHERE book_uuid = ?
                """, (book_uuid,))
                
                rows = cursor.fetchall()
                
                if not rows:
                    logger.warning(f"  ⚠️  No position entries for {book_title}")
                    return False

                # Generate coordinated timestamps
                timestamp_ms, updated_at_str = self._get_timestamp_formats()
                
                updates_made = 0
                
                for row in rows:
                    user_id = row['user_id']
                    position_uuid = row['uuid']
                    
                    # Parse existing locator JSON
                    try:
                        locator = json.loads(row['locator'])
                    except:
                        locator = {}
                    
                    # Ensure locations structure exists
                    if 'locations' not in locator:
                        locator['locations'] = {}
                    
                    # Update the progress percentage
                    locator['locations']['totalProgression'] = float(percentage)
                    
                    # UPDATE POSITION TABLE (both timestamp columns)
                    cursor.execute("""
                        UPDATE position 
                        SET locator = ?, 
                            timestamp = ?, 
                            updated_at = ? 
                        WHERE uuid = ?
                    """, (json.dumps(locator), timestamp_ms, updated_at_str, position_uuid))
                    
                    # UPDATE SESSION TABLE (user's most recent session)
                    self._update_session(conn, user_id, updated_at_str)
                    
                    updates_made += 1
                    logger.debug(f"  💾 Position {position_uuid[:8]}... → {percentage:.2%}")
                    logger.debug(f"     timestamp={timestamp_ms:.0f}, updated_at='{updated_at_str}'")
                
                logger.info(f"✅ Storyteller DB Sync: {book_title} → {percentage:.2%} ({updates_made} update(s))")
                return True
                
        except Exception as e:
            logger.error(f"❌ Storyteller DB Write Error: {e}", exc_info=True)
            return False

    def get_progress(self, ebook_filename):
        """
        REQUIRED BY main.py: Returns (percentage, timestamp)
        
        Args:
            ebook_filename: Name of the ebook file
            
        Returns:
            tuple: (percentage as float 0.0-1.0, timestamp in seconds)
                   or (None, 0) if not found
        """
        try:
            with self._get_connection() as conn:
                book_uuid, book_title = self._find_book_uuid(conn, ebook_filename)
                
                if not book_uuid: 
                    return None, 0

                cursor = conn.cursor()
                
                # Get most recent position for this book
                cursor.execute("""
                    SELECT locator, timestamp, updated_at 
                    FROM position 
                    WHERE book_uuid = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                """, (book_uuid,))
                
                row = cursor.fetchone()
                
                if row:
                    # Parse locator JSON
                    try:
                        locator = json.loads(row['locator'])
                    except:
                        locator = {}
                    
                    # Extract progress percentage
                    pct = float(locator.get('locations', {}).get('totalProgression', 0.0))
                    
                    # Convert timestamp from milliseconds to seconds
                    ts = float(row['timestamp']) / 1000.0 if row['timestamp'] else 0.0
                    
                    logger.debug(f"📖 Storyteller: {book_title} @ {pct:.2%}")
                    
                    return pct, ts
                    
        except Exception as e:
            logger.error(f"❌ Storyteller DB Read Error: {e}")
            
        return None, 0

    def get_progress_with_fragment(self, ebook_filename):
        """
        Returns (percentage, timestamp, href, fragment_id) for precise text extraction.
        
        Use this when you need the exact sentence/paragraph location from Storyteller
        for more accurate transcript matching.
        
        Args:
            ebook_filename: Name of the ebook file
            
        Returns:
            tuple: (pct, ts, href, fragment_id) 
                   - pct: percentage as float 0.0-1.0
                   - ts: timestamp in seconds
                   - href: EPUB internal file path (e.g., "OPS/s065-Chapter-048.xhtml")
                   - fragment_id: Sentence ID (e.g., "s065-sentence186")
                   
                   or (None, 0, None, None) if not found
        """
        try:
            with self._get_connection() as conn:
                book_uuid, book_title = self._find_book_uuid(conn, ebook_filename)
                
                if not book_uuid: 
                    return None, 0, None, None

                cursor = conn.cursor()
                
                # Get most recent position for this book
                cursor.execute("""
                    SELECT locator, timestamp, updated_at 
                    FROM position 
                    WHERE book_uuid = ? 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                """, (book_uuid,))
                
                row = cursor.fetchone()
                
                if row:
                    # Parse locator JSON
                    try:
                        locator = json.loads(row['locator'])
                    except:
                        locator = {}
                    
                    # Extract progress percentage
                    pct = float(locator.get('locations', {}).get('totalProgression', 0.0))
                    
                    # Convert timestamp from milliseconds to seconds
                    ts = float(row['timestamp']) / 1000.0 if row['timestamp'] else 0.0
                    
                    # Extract precise location markers for fragment-based text extraction
                    href = locator.get('href')  # EPUB internal file path
                    fragments = locator.get('locations', {}).get('fragments', [])
                    fragment_id = fragments[0] if fragments else None  # First fragment ID
                    
                    logger.debug(f"📖 Storyteller: {book_title} @ {pct:.2%}, Fragment: {fragment_id}")
                    
                    return pct, ts, href, fragment_id
                    
        except Exception as e:
            logger.error(f"❌ Storyteller DB Read Error: {e}")
            
        return None, 0, None, None