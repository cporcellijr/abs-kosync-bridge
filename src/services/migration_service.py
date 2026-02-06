"""
Migration Service.
Handles legacy data migration from file-based storage to SQLite.
"""

import logging
import json
from pathlib import Path

from src.services.alignment_service import AlignmentService
from src.db.models import BookAlignment

logger = logging.getLogger(__name__)

class MigrationService:
    def __init__(self, database_service, alignment_service: AlignmentService, data_dir: Path):
        self.db = database_service
        self.alignment = alignment_service
        self.transcripts_dir = data_dir / "transcripts"

    def migrate_legacy_data(self):
        """
        Migrate legacy JSON transcript files to the database.
        
        Strategy:
        1. Look for *.json in transcripts/
        2. Check if we already have an entry in 'book_alignments' table.
        3. If not, we can't easily "align" without the book text!
           Legacy files only contain the transcript segments.
           
           Option A: Load the transcript into a temporary structure? No, we want unified structure.
           Option B: Mark them for re-processing?
           Option C: If we have an associated alignment map file (legacy *_alignment.json), import that!
           
        Refined Strategy:
        - Check for {abs_id}_alignment.json (Used in the interim version).
        - If found, import that directly into DB.
        - If only {abs_id}.json exists (Raw Transcript), we mostly leave it alone or back it up.
          The system will re-generate alignment when it next processes the book, as it needs the Ebook Text to created the anchors.
        """
        if not self.transcripts_dir.exists():
            return


        
        files = list(self.transcripts_dir.glob("*_alignment.json"))
        if not files:
            logger.info("Migration: No legacy alignment maps found.")
            return

        logger.info(f"Migration: Found {len(files)} legacy alignment maps. Processing...")
        
        with self.db.get_session() as session:
            count = 0
            for map_file in files:
                abs_id = map_file.stem.replace('_alignment', '')
                
                # Check DB
                exists = session.query(BookAlignment).filter_by(abs_id=abs_id).first()
                if exists:
                    # Already migrated or new data exists
                    self._delete_legacy_file(map_file)
                    continue
                
                try:
                     with open(map_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        
                     # Validate format (list of dicts)
                     if isinstance(data, list) and len(data) > 0 and 'char' in data[0]:
                         new_entry = BookAlignment(abs_id=abs_id, alignment_map_json=json.dumps(data))
                         session.add(new_entry)
                         count += 1
                         self._delete_legacy_file(map_file)
                     else:
                         logger.warning(f"Skipping invalid alignment file: {map_file.name}")
                
                except Exception as e:
                    logger.error(f"Failed to migrate {map_file.name}: {e}")

            if count > 0:
                logger.info(f"✅ Migrated {count} legacy alignment maps to database.")

    def _delete_legacy_file(self, file_path: Path):
        """Delete legacy file after successful migration."""
        try:
            file_path.unlink()
            logger.debug(f"Deleted legacy file: {file_path.name}")
        except Exception as e:
            logger.warning(f"Failed to delete legacy file {file_path.name}: {e}")
