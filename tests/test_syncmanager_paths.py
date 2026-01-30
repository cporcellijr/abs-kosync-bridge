#!/usr/bin/env python3
"""
Quick test to verify SyncManager gets paths from DI container instead of hardcoded values.
"""

import sys
import os
import tempfile
import shutil

# Add project root to path
project_root = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, project_root)

def test_syncmanager_di_paths():
    print("[TEST] Testing SyncManager paths from DI container...")

    # Create temporary directories for testing
    temp_base_dir = tempfile.mkdtemp(prefix="abs_kosync_test_")
    temp_data_dir = os.path.join(temp_base_dir, "data")
    temp_books_dir = os.path.join(temp_base_dir, "books")

    # Create the directories
    os.makedirs(temp_data_dir, exist_ok=True)
    os.makedirs(temp_books_dir, exist_ok=True)

    print(f"[INIT] Created temp data dir: {temp_data_dir}")
    print(f"[INIT] Created temp books dir: {temp_books_dir}")

    # Set test environment variables to use temp directories
    os.environ['DATA_DIR'] = temp_data_dir
    os.environ['BOOKS_DIR'] = temp_books_dir

    try:
        # Test container creation
        print("\n[INIT] Creating DI container...")
        from src.utils.di_container import create_container
        container = create_container()
        print("[OK] Container created successfully")

        # Test SyncManager creation
        print("\n[TEST] Testing SyncManager with DI paths...")
        sync_manager = container.sync_manager()
        print(f"[OK] SyncManager created: {type(sync_manager).__name__}")

        # Verify paths are correctly injected
        print("\n[VERIFY] Verifying injected paths...")
        print(f"[INFO] Data dir: {sync_manager.data_dir}")
        print(f"[INFO] Books dir: {sync_manager.books_dir}")
        print(f"[INFO] EPUB cache dir: {sync_manager.epub_cache_dir}")

        # Verify they match our environment variables (normalize paths for comparison)
        from pathlib import Path
        expected_data_dir = Path(temp_data_dir).resolve()
        expected_books_dir = Path(temp_books_dir).resolve()
        actual_data_dir = Path(sync_manager.data_dir).resolve()
        actual_books_dir = Path(sync_manager.books_dir).resolve()

        if actual_data_dir == expected_data_dir:
            print("[OK] Data dir correctly injected from DI container")
        else:
            print(f"[FAIL] Data dir mismatch: expected {expected_data_dir}, got {actual_data_dir}")
            return False

        if actual_books_dir == expected_books_dir:
            print("[OK] Books dir correctly injected from DI container")
        else:
            print(f"[FAIL] Books dir mismatch: expected {expected_books_dir}, got {actual_books_dir}")
            return False

        # Check that epub_cache_dir is based on data_dir
        expected_cache_dir = expected_data_dir / "epub_cache"
        actual_cache_dir = Path(sync_manager.epub_cache_dir).resolve()
        if actual_cache_dir == expected_cache_dir:
            print("[OK] EPUB cache dir correctly derived from data dir")
        else:
            print(f"[FAIL] EPUB cache dir mismatch: expected {expected_cache_dir}, got {actual_cache_dir}")
            return False

        print("\n[PASS] All SyncManager DI path tests passed!")
        return True

    except Exception as e:
        print(f"\n[FAIL] Test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        # Close logging handlers to release file locks on Windows
        import logging
        logging.shutdown()
        for handler in logging.root.handlers[:]:
            handler.close()
            logging.root.removeHandler(handler)
            
        if 'container' in locals():
            try:
                # Retrieve the database service singleton (if initialized)
                db_service = container.database_service()
                if hasattr(db_service, 'db_manager'):
                    print("[CLEAN] Closing database connection...")
                    db_service.db_manager.close()
            except Exception as e:
                print(f"[WARN] Failed to close database: {e}")
                
        # Clean up temporary directories
        if os.path.exists(temp_base_dir):
            try:
                shutil.rmtree(temp_base_dir)
                print(f"[CLEAN] Cleaned up temp directory: {temp_base_dir}")
            except Exception as e:
                 print(f"[WARN] Failed to cleanup temp dir: {e}")

if __name__ == "__main__":
    success = test_syncmanager_di_paths()
    sys.exit(0 if success else 1)
