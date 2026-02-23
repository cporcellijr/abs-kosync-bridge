#!/usr/bin/env python3
import sys
import os
from pathlib import Path
import tempfile
import shutil
from unittest.mock import MagicMock

# Add project root to path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

print(f"DEBUG: PYTHONPATH: {sys.path[0]}")

try:
    from src.sync_manager import SyncManager
    from src.db.models import Book, PendingSuggestion
    print("DEBUG: Imports successful")
except Exception as e:
    print(f"ERROR: Import failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

def run_test():
    temp_dir = tempfile.mkdtemp()
    try:
        cache_dir = Path(temp_dir) / "epub_cache"
        cache_dir.mkdir()
        
        # Create some dummy files
        valid_file1 = "valid1.epub"
        valid_file2 = "valid2.epub"
        orphaned_file = "orphaned.epub"
        
        (cache_dir / valid_file1).write_text("content")
        (cache_dir / valid_file2).write_text("content")
        (cache_dir / orphaned_file).write_text("content")
        
        print(f"DEBUG: Created files in {cache_dir}")
        
        # Mock database service
        db_service = MagicMock()
        
        # Setup mocks for books and suggestions
        book1 = MagicMock(spec=Book)
        book1.ebook_filename = valid_file1
        
        # Mock PendingSuggestion
        suggestion1 = MagicMock(spec=PendingSuggestion)
        # Instead of spec=PendingSuggestion for mocks that use properties, just use MagicMock
        suggestion1 = MagicMock()
        suggestion1.matches = [{"filename": valid_file2}]
        
        db_service.get_all_books.return_value = [book1]
        db_service.get_all_pending_suggestions.return_value = [suggestion1]
        
        # Mock other required methods for __init__
        db_service.get_books_by_status.return_value = []
        
        print("DEBUG: Initializing SyncManager...")
        # Initialize SyncManager with mocks
        sync_manager = SyncManager(
            database_service=db_service,
            sync_clients={}, # Avoid connection checks
            epub_cache_dir=cache_dir
        )
        print("DEBUG: SyncManager initialized")

        # Run cleanup
        print("DEBUG: Running cleanup_cache...")
        sync_manager.cleanup_cache()
        
        # Verify files
        v1_exists = (cache_dir / valid_file1).exists()
        v2_exists = (cache_dir / valid_file2).exists()
        orph_exists = (cache_dir / orphaned_file).exists()
        
        print(f"DEBUG: valid1 exists: {v1_exists}")
        print(f"DEBUG: valid2 exists: {v2_exists}")
        print(f"DEBUG: orphaned exists: {orph_exists}")
        
        assert v1_exists, "Valid file 1 should still exist"
        assert v2_exists, "Valid file 2 should still exist"
        assert not orph_exists, "Orphaned file should have been deleted"
        
        print("\n[PASS] Cache cleanup test passed!")
        return True

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(temp_dir)

if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
