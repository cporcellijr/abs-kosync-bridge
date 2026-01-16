"""
SQLAlchemy Database Wrapper that provides JsonDB-compatible interface.
This allows for seamless migration from JSON to SQLAlchemy ORM without changing existing code.
"""

import logging
from typing import Dict, Any, Callable
from pathlib import Path

from src.db.database_service import DatabaseService, DatabaseMigrator
from src.db.models import Book, State, Job

logger = logging.getLogger(__name__)


class SQLiteJsonDBWrapper:
    """
    Wrapper around unified DatabaseService that provides JsonDB-compatible interface.

    This class maintains backward compatibility with the existing JsonDB interface
    while using SQLAlchemy ORM models as the underlying storage mechanism.
    """

    def __init__(self, filepath: str):
        self.filepath = Path(filepath)
        self.file_type = self._determine_file_type()

        # Create database service with SQLAlchemy
        db_dir = self.filepath.parent
        if self.file_type in ['mapping', 'state']:
            self.db_service = DatabaseService(str(db_dir / "database.db"))
        else:
            # Generic case
            db_path = self.filepath.with_suffix('.db')
            self.db_service = DatabaseService(str(db_path))

        # Perform migration if needed
        self._migrate_if_needed()

    def _determine_file_type(self) -> str:
        """Determine the type of JSON file based on filename."""
        filename = self.filepath.name.lower()
        if 'mapping' in filename:
            return 'mapping'
        elif 'state' in filename:
            return 'state'
        else:
            return 'unknown'

    def _migrate_if_needed(self):
        """Migrate from JSON files if they exist and SQLAlchemy database is empty."""
        if self.file_type == 'mapping':
            # For mapping files, we need to check both mapping and state files
            state_path = self.filepath.parent / "last_state.json"
            migrator = DatabaseMigrator(
                self.db_service,
                str(self.filepath),
                str(state_path)
            )
        elif self.file_type == 'state':
            # For state files, we need to check both mapping and state files
            mapping_path = self.filepath.parent / "mapping_db.json"
            migrator = DatabaseMigrator(
                self.db_service,
                str(mapping_path),
                str(self.filepath)
            )
        else:
            # Generic migration - try to find related files
            migrator = DatabaseMigrator(
                self.db_service,
                str(self.filepath),
                str(self.filepath)
            )

        if migrator.should_migrate():
            logger.info(f"Migrating {self.filepath} to SQLAlchemy database...")
            migrator.migrate()

    def load(self, default=None):
        """
        Load data in JsonDB-compatible format.
        Returns default if no data exists.
        """
        if default is None:
            default = {}

        try:
            if self.file_type == 'mapping':
                # Return mappings in the original JSON format
                mappings = self._get_mappings_as_dict()
                return {"mappings": mappings} if mappings else default

            elif self.file_type == 'state':
                # Return state data in the original JSON format
                states = self._get_states_as_dict()
                return states if states else default

            else:
                # Generic case - return empty default
                logger.warning(f"Unknown file type for {self.filepath}, returning default")
                return default

        except Exception as e:
            logger.error(f"Failed to load data from SQLAlchemy database: {e}")
            return default

    def save(self, data: Dict[str, Any]) -> bool:
        """
        Save data in JsonDB-compatible format.
        Converts the data to the appropriate SQLAlchemy operations.
        """
        try:
            if self.file_type == 'mapping':
                # Handle mapping data
                if 'mappings' in data:
                    self._save_mappings_from_dict(data['mappings'])
                    return True
                else:
                    logger.warning("Invalid mapping data format")
                    return False

            elif self.file_type == 'state':
                # Handle state data
                self._save_states_from_dict(data)
                return True

            else:
                logger.warning(f"Unknown file type for {self.filepath}, cannot save")
                return False

        except Exception as e:
            logger.error(f"Failed to save data to SQLAlchemy database: {e}")
            return False

    def update(self, update_func: Callable, default=None) -> bool:
        """
        Atomic read-modify-write operation in JsonDB-compatible format.
        """
        if default is None:
            default = {}

        try:
            # Load current data
            current_data = self.load(default)

            # Apply update function
            updated_data = update_func(current_data)

            # Save updated data
            return self.save(updated_data)

        except Exception as e:
            logger.error(f"Failed to update data in SQLAlchemy database: {e}")
            return False

    def _get_mappings_as_dict(self) -> list:
        """Convert Book models to dictionary format for compatibility."""
        books = self.db_service.get_all_books()
        mappings = []

        for book in books:
            mapping = {
                'abs_id': book.abs_id,
                'abs_title': book.abs_title,
                'ebook_filename': book.ebook_filename,
                'kosync_doc_id': book.kosync_doc_id,
                'transcript_file': book.transcript_file,
                'status': book.status,
                'duration': book.duration
            }

            # Add latest job data if it exists
            latest_job = self.db_service.get_latest_job(book.abs_id)
            if latest_job:
                mapping.update({
                    'last_attempt': latest_job.last_attempt,
                    'retry_count': latest_job.retry_count,
                    'last_error': latest_job.last_error
                })

            mappings.append(mapping)

        return mappings

    def _save_mappings_from_dict(self, mappings_list: list):
        """Convert dictionary mappings to Book models and save."""
        for mapping in mappings_list:
            book = Book(
                abs_id=mapping['abs_id'],
                abs_title=mapping.get('abs_title'),
                ebook_filename=mapping.get('ebook_filename'),
                kosync_doc_id=mapping.get('kosync_doc_id'),
                transcript_file=mapping.get('transcript_file'),
                status=mapping.get('status', 'active'),
                duration=mapping.get('duration')
            )
            self.db_service.save_book(book)

            # Also save job data if present
            if any(key in mapping for key in ['last_attempt', 'retry_count', 'last_error']):
                job = Job(
                    abs_id=mapping['abs_id'],
                    last_attempt=mapping.get('last_attempt'),
                    retry_count=mapping.get('retry_count', 0),
                    last_error=mapping.get('last_error')
                )
                self.db_service.save_job(job)

    def _get_states_as_dict(self) -> dict:
        """Convert State models to dictionary format for compatibility."""
        states = self.db_service.get_all_states()
        result = {}

        # Group states by abs_id
        for state in states:
            if state.abs_id not in result:
                result[state.abs_id] = {}

            # Map client names to the expected format
            if state.client_name == 'kosync':
                result[state.abs_id]['kosync_pct'] = state.percentage
                if state.xpath:
                    result[state.abs_id]['kosync_xpath'] = state.xpath
            elif state.client_name == 'abs':
                result[state.abs_id]['abs_pct'] = state.percentage
                if state.timestamp:
                    result[state.abs_id]['abs_ts'] = state.timestamp
            elif state.client_name == 'absebook':
                result[state.abs_id]['absebook_pct'] = state.percentage
                if state.cfi:
                    result[state.abs_id]['absebook_cfi'] = state.cfi
            elif state.client_name == 'storyteller':
                result[state.abs_id]['storyteller_pct'] = state.percentage
                if state.xpath:
                    result[state.abs_id]['storyteller_xpath'] = state.xpath
                if state.cfi:
                    result[state.abs_id]['storyteller_cfi'] = state.cfi
            elif state.client_name == 'booklore':
                result[state.abs_id]['booklore_pct'] = state.percentage
                if state.xpath:
                    result[state.abs_id]['booklore_xpath'] = state.xpath
                if state.cfi:
                    result[state.abs_id]['booklore_cfi'] = state.cfi

            # Set last_updated from any state record
            if state.last_updated:
                result[state.abs_id]['last_updated'] = state.last_updated

        return result

    def _save_states_from_dict(self, state_dict: dict):
        """Convert dictionary states to State models and save."""
        for abs_id, data in state_dict.items():
            last_updated = data.get('last_updated')

            # Handle kosync data
            if 'kosync_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='kosync',
                    last_updated=last_updated,
                    percentage=data['kosync_pct'],
                    xpath=data.get('kosync_xpath')
                )
                self.db_service.save_state(state)

            # Handle ABS data
            if 'abs_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='abs',
                    last_updated=last_updated,
                    percentage=data['abs_pct'],
                    timestamp=data.get('abs_ts')
                )
                self.db_service.save_state(state)

            # Handle ABS ebook data
            if 'absebook_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='absebook',
                    last_updated=last_updated,
                    percentage=data['absebook_pct'],
                    cfi=data.get('absebook_cfi')
                )
                self.db_service.save_state(state)

            # Handle Storyteller data
            if 'storyteller_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='storyteller',
                    last_updated=last_updated,
                    percentage=data['storyteller_pct'],
                    xpath=data.get('storyteller_xpath'),
                    cfi=data.get('storyteller_cfi')
                )
                self.db_service.save_state(state)

            # Handle Booklore data
            if 'booklore_pct' in data:
                state = State(
                    abs_id=abs_id,
                    client_name='booklore',
                    last_updated=last_updated,
                    percentage=data['booklore_pct'],
                    xpath=data.get('booklore_xpath'),
                    cfi=data.get('booklore_cfi')
                )
                self.db_service.save_state(state)


# For backward compatibility - alias the wrapper class
JsonDB = SQLiteJsonDBWrapper
