"""
SQLite Database implementation for abs-kosync-bridge.
Replaces JSON file storage with proper relational database structure.
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from contextlib import contextmanager
import threading

logger = logging.getLogger(__name__)


class SQLiteDB:
    """
    SQLite database handler for abs-kosync-bridge.

    Tables:
    - state: Stores sync state per book and client
    - book: Stores book metadata and mapping information
    - job: Stores job execution data for books
    """

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_database()

    def _init_database(self):
        """Initialize the database schema if it doesn't exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # State table - stores sync state per book and client
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    abs_id TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    last_updated REAL,
                    percentage REAL,
                    timestamp REAL,
                    xpath TEXT,
                    cfi TEXT,
                    UNIQUE(abs_id, client_name)
                )
            """)

            # Book table - stores book metadata and mapping
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS book (
                    abs_id TEXT PRIMARY KEY,
                    abs_title TEXT,
                    ebook_filename TEXT,
                    kosync_doc_id TEXT,
                    transcript_file TEXT,
                    status TEXT DEFAULT 'active',
                    abs_session_id TEXT
                )
            """)

            # Job table - stores job execution data
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS job (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    abs_id TEXT NOT NULL,
                    last_attempt REAL,
                    retry_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    FOREIGN KEY (abs_id) REFERENCES book (abs_id) ON DELETE CASCADE
                )
            """)

            # Create indices for better performance
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_state_abs_id ON state(abs_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_job_abs_id ON job(abs_id)")

            conn.commit()

    @contextmanager
    def _get_connection(self):
        """Get a database connection with proper locking."""
        with self._lock:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
            finally:
                conn.close()

    # State table methods
    def get_state(self, abs_id: str) -> Dict[str, Any]:
        """Get all state data for a specific book ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT client_name, last_updated, percentage, timestamp, xpath, cfi
                FROM state WHERE abs_id = ?
            """, (abs_id,))

            rows = cursor.fetchall()
            result = {}

            for row in rows:
                client_data = {
                    'last_updated': row['last_updated'],
                    'percentage': row['percentage'],
                    'timestamp': row['timestamp']
                }

                # Add client-specific fields if they exist
                if row['xpath']:
                    client_data['xpath'] = row['xpath']
                if row['cfi']:
                    client_data['cfi'] = row['cfi']

                # Map client names to the expected format
                if row['client_name'] == 'kosync':
                    result['kosync_pct'] = row['percentage']
                    if row['xpath']:
                        result['kosync_xpath'] = row['xpath']
                elif row['client_name'] == 'abs':
                    result['abs_pct'] = row['percentage']
                    result['abs_ts'] = row['timestamp']
                elif row['client_name'] == 'absebook':
                    result['absebook_pct'] = row['percentage']
                    if row['cfi']:
                        result['absebook_cfi'] = row['cfi']

                if row['last_updated']:
                    result['last_updated'] = row['last_updated']

            return result

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """Get all state data formatted like the old JSON structure."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT abs_id FROM state")
            abs_ids = [row['abs_id'] for row in cursor.fetchall()]

            result = {}
            for abs_id in abs_ids:
                state_data = self.get_state(abs_id)
                if state_data:
                    result[abs_id] = state_data

            return result

    def save_state(self, abs_id: str, client_name: str, **kwargs):
        """Save state for a specific book and client."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Prepare data
            last_updated = kwargs.get('last_updated')
            percentage = kwargs.get('percentage')
            timestamp = kwargs.get('timestamp')
            xpath = kwargs.get('xpath')
            cfi = kwargs.get('cfi')

            cursor.execute("""
                INSERT OR REPLACE INTO state 
                (abs_id, client_name, last_updated, percentage, timestamp, xpath, cfi)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (abs_id, client_name, last_updated, percentage, timestamp, xpath, cfi))

            conn.commit()

    def update_state_from_dict(self, state_dict: Dict[str, Dict[str, Any]]):
        """Update state from a dictionary (used for JSON migration)."""
        for abs_id, data in state_dict.items():
            # Extract last_updated that applies to all clients
            last_updated = data.get('last_updated')

            # Handle kosync data
            if 'kosync_pct' in data:
                self.save_state(
                    abs_id, 'kosync',
                    last_updated=last_updated,
                    percentage=data['kosync_pct'],
                    xpath=data.get('kosync_xpath')
                )

            # Handle ABS data
            if 'abs_pct' in data:
                self.save_state(
                    abs_id, 'abs',
                    last_updated=last_updated,
                    percentage=data['abs_pct'],
                    timestamp=data.get('abs_ts')
                )

            # Handle ABS ebook data
            if 'absebook_pct' in data:
                self.save_state(
                    abs_id, 'absebook',
                    last_updated=last_updated,
                    percentage=data['absebook_pct'],
                    cfi=data.get('absebook_cfi')
                )

    # Book table methods
    def get_book(self, abs_id: str) -> Optional[Dict[str, Any]]:
        """Get book data by abs_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT abs_id, abs_title, ebook_filename, kosync_doc_id, 
                       transcript_file, status, abs_session_id
                FROM book WHERE abs_id = ?
            """, (abs_id,))

            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def get_all_books(self) -> List[Dict[str, Any]]:
        """Get all books."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT abs_id, abs_title, ebook_filename, kosync_doc_id, 
                       transcript_file, status, abs_session_id
                FROM book
            """)

            return [dict(row) for row in cursor.fetchall()]

    def save_book(self, abs_id: str, abs_title: str = None, ebook_filename: str = None,
                  kosync_doc_id: str = None, transcript_file: str = None,
                  status: str = 'active', abs_session_id: str = None):
        """Save or update book data."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO book 
                (abs_id, abs_title, ebook_filename, kosync_doc_id, transcript_file, status, abs_session_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (abs_id, abs_title, ebook_filename, kosync_doc_id, transcript_file, status, abs_session_id))

            conn.commit()

    def get_mappings(self) -> List[Dict[str, Any]]:
        """Get all mappings in the old JSON format for compatibility."""
        books = self.get_all_books()
        mappings = []

        for book in books:
            mapping = {
                'abs_id': book['abs_id'],
                'abs_title': book['abs_title'],
                'ebook_filename': book['ebook_filename'],
                'kosync_doc_id': book['kosync_doc_id'],
                'transcript_file': book['transcript_file'],
                'status': book['status']
            }

            if book['abs_session_id']:
                mapping['abs_session_id'] = book['abs_session_id']

            # Add job data if it exists
            job_data = self.get_latest_job(book['abs_id'])
            if job_data:
                mapping.update({
                    'last_attempt': job_data['last_attempt'],
                    'retry_count': job_data['retry_count'],
                    'last_error': job_data['last_error']
                })

            mappings.append(mapping)

        return mappings

    def update_books_from_mappings(self, mappings_list: List[Dict[str, Any]]):
        """Update books from mappings list (used for JSON migration)."""
        for mapping in mappings_list:
            self.save_book(
                abs_id=mapping['abs_id'],
                abs_title=mapping.get('abs_title'),
                ebook_filename=mapping.get('ebook_filename'),
                kosync_doc_id=mapping.get('kosync_doc_id'),
                transcript_file=mapping.get('transcript_file'),
                status=mapping.get('status', 'active'),
                abs_session_id=mapping.get('abs_session_id')
            )

            # Also save job data if present
            if any(key in mapping for key in ['last_attempt', 'retry_count', 'last_error']):
                self.save_job(
                    abs_id=mapping['abs_id'],
                    last_attempt=mapping.get('last_attempt'),
                    retry_count=mapping.get('retry_count', 0),
                    last_error=mapping.get('last_error')
                )

    # Job table methods
    def get_latest_job(self, abs_id: str) -> Optional[Dict[str, Any]]:
        """Get the latest job data for a book."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT last_attempt, retry_count, last_error
                FROM job WHERE abs_id = ? 
                ORDER BY last_attempt DESC LIMIT 1
            """, (abs_id,))

            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def save_job(self, abs_id: str, last_attempt: float = None,
                 retry_count: int = 0, last_error: str = None):
        """Save job execution data."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO job (abs_id, last_attempt, retry_count, last_error)
                VALUES (?, ?, ?, ?)
            """, (abs_id, last_attempt, retry_count, last_error))

            conn.commit()

    def update_job(self, abs_id: str, last_attempt: float = None,
                   retry_count: int = None, last_error: str = None):
        """Update the latest job for a book."""
        # Get the latest job ID
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM job WHERE abs_id = ? 
                ORDER BY last_attempt DESC LIMIT 1
            """, (abs_id,))

            row = cursor.fetchone()
            if row:
                # Update existing job
                job_id = row['id']
                updates = []
                values = []

                if last_attempt is not None:
                    updates.append("last_attempt = ?")
                    values.append(last_attempt)

                if retry_count is not None:
                    updates.append("retry_count = ?")
                    values.append(retry_count)

                if last_error is not None:
                    updates.append("last_error = ?")
                    values.append(last_error)

                if updates:
                    values.append(job_id)
                    cursor.execute(f"""
                        UPDATE job SET {', '.join(updates)}
                        WHERE id = ?
                    """, values)
                    conn.commit()
            else:
                # Create new job
                self.save_job(abs_id, last_attempt, retry_count, last_error)


class DatabaseMigrator:
    """Handles migration from JSON files to SQLite database."""

    def __init__(self, sqlite_db: SQLiteDB, json_db_path: str, json_state_path: str):
        self.sqlite_db = sqlite_db
        self.json_db_path = Path(json_db_path)
        self.json_state_path = Path(json_state_path)

    def migrate(self):
        """Perform migration from JSON to SQLite."""
        logger.info("Starting migration from JSON to SQLite...")

        # Migrate mappings/books
        if self.json_db_path.exists():
            try:
                with open(self.json_db_path, 'r') as f:
                    mapping_data = json.load(f)

                if 'mappings' in mapping_data:
                    self.sqlite_db.update_books_from_mappings(mapping_data['mappings'])
                    logger.info(f"Migrated {len(mapping_data['mappings'])} book mappings")

            except Exception as e:
                logger.error(f"Failed to migrate mapping data: {e}")

        # Migrate state
        if self.json_state_path.exists():
            try:
                with open(self.json_state_path, 'r') as f:
                    state_data = json.load(f)

                self.sqlite_db.update_state_from_dict(state_data)
                logger.info(f"Migrated state for {len(state_data)} books")

            except Exception as e:
                logger.error(f"Failed to migrate state data: {e}")

        logger.info("Migration completed")

    def should_migrate(self) -> bool:
        """Check if migration is needed (JSON files exist but no data in SQLite)."""
        # Check if we have any books in SQLite
        books = self.sqlite_db.get_all_books()
        if books:
            return False  # Already have data, no migration needed

        # Check if JSON files exist
        return (self.json_db_path.exists() or self.json_state_path.exists())
