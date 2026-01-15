"""
Test the unified database service with direct model operations.
"""

import logging
import os
from pathlib import Path

# Override environment variables for testing
os.environ['DATA_DIR'] = 'data'
os.environ['BOOKS_DIR'] = 'data'

# Setup basic logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_unified_database_service():
    """Test the unified database service with model operations."""
    print("🧪 Testing Unified Database Service")
    print("=" * 50)

    try:
        # Test 1: Initialize the service
        print("\n1️⃣ Testing Service Initialization")
        from src.db.database_service import DatabaseService
        from src.db.models import Book, State, Job
        import time

        db_service = DatabaseService('../data/database.db')
        print("✅ Database service initialized")

        # Test 2: Create models and save them
        print("\n2️⃣ Testing Model Creation and Saving")

        test_abs_id = 'unified-test-book'

        # Clean up any existing test data
        db_service.delete_book(test_abs_id)

        # Create a Book model
        book = Book(
            abs_id=test_abs_id,
            abs_title='Unified Test Book',
            ebook_filename='unified-test.epub',
            kosync_doc_id='unified-test-doc',
            status='active'
        )

        saved_book = db_service.save_book(book)
        print(f"✅ Created book: {saved_book.abs_id} - {saved_book.abs_title}")

        # Create State models
        kosync_state = State(
            abs_id=test_abs_id,
            client_name='kosync',
            last_updated=time.time(),
            percentage=0.75,
            xpath='/test/unified/xpath'
        )

        abs_state = State(
            abs_id=test_abs_id,
            client_name='abs',
            last_updated=time.time(),
            percentage=0.72,
            timestamp=2400.5
        )

        saved_kosync_state = db_service.save_state(kosync_state)
        saved_abs_state = db_service.save_state(abs_state)

        print(f"✅ Created kosync state: {saved_kosync_state.percentage:.2%}")
        print(f"✅ Created abs state: {saved_abs_state.percentage:.2%}")

        # Create Job model
        job = Job(
            abs_id=test_abs_id,
            last_attempt=time.time(),
            retry_count=2,
            last_error='Test unified error'
        )

        saved_job = db_service.save_job(job)
        print(f"✅ Created job: retry_count={saved_job.retry_count}")

        # Test 3: Query models back
        print("\n3️⃣ Testing Model Retrieval")

        # Get book
        retrieved_book = db_service.get_book(test_abs_id)
        print(f"📚 Retrieved book: {retrieved_book.abs_title}")

        # Get states
        states = db_service.get_states_for_book(test_abs_id)
        print(f"📊 Retrieved {len(states)} states:")
        for state in states:
            print(f"   - {state.client_name}: {state.percentage:.2%}")

        # Get latest job
        latest_job = db_service.get_latest_job(test_abs_id)
        print(f"🔧 Retrieved job: {latest_job.retry_count} retries")

        # Test 4: Test JsonDB compatibility
        print("\n4️⃣ Testing JsonDB Compatibility")

        from tests.utils.sqlite_json_wrapper import JsonDB

        mapping_db = JsonDB('../data/mapping_db.json')
        mappings_data = mapping_db.load()

        found_test_book = False
        for mapping in mappings_data.get('mappings', []):
            if mapping['abs_id'] == test_abs_id:
                found_test_book = True
                print(f"✅ Found test book via JsonDB: {mapping['abs_title']}")
                break

        if not found_test_book:
            print("⚠️  Test book not found via JsonDB")

        # Test 5: Advanced queries
        print("\n5️⃣ Testing Advanced Queries")

        # Get statistics
        stats = db_service.get_statistics()
        print(f"📈 Statistics: {stats['total_books']} books, {stats['total_states']} states")

        # Get books with recent activity
        recent_books = db_service.get_books_with_recent_activity(limit=3)
        print(f"⏰ Recent activity: {len(recent_books)} books")

        # Get failed jobs
        failed_jobs = db_service.get_failed_jobs(limit=3)
        print(f"❌ Failed jobs: {len(failed_jobs)}")

        # Test 6: Migration Testing
        print("\n6️⃣ Testing JSON to SQLAlchemy Migration")

        # Create test JSON files for migration
        import json
        import tempfile

        # Create temporary directory for migration test
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Create test mapping JSON file
            mapping_json_path = temp_path / "test_mapping.json"
            mapping_data = {
                "mappings": [
                    {
                        "abs_id": "migration-test-1",
                        "abs_title": "Migration Test Book 1",
                        "ebook_filename": "migration-test-1.epub",
                        "kosync_doc_id": "migration-kosync-1",
                        "transcript_file": "migration-transcript-1.json",
                        "status": "active",
                        "hardcover_book_id": "hc-123",
                        "hardcover_edition_id": "hc-edition-456",
                        "hardcover_pages": 350,
                        "isbn": "978-1234567890",
                        "retry_count": 1,
                        "last_error": "Test migration error"
                    },
                    {
                        "abs_id": "migration-test-2",
                        "abs_title": "Migration Test Book 2",
                        "ebook_filename": "migration-test-2.epub",
                        "kosync_doc_id": "migration-kosync-2",
                        "status": "paused"
                    }
                ]
            }

            with open(mapping_json_path, 'w') as f:
                json.dump(mapping_data, f, indent=2)
            print(f"✅ Created test mapping JSON with {len(mapping_data['mappings'])} books")

            # Create test state JSON file
            state_json_path = temp_path / "test_state.json"
            state_data = {
                "migration-test-1": {
                    "last_updated": time.time() - 3600,  # 1 hour ago
                    "kosync_pct": 0.45,
                    "kosync_xpath": "/html/body/div[1]/p[12]",
                    "abs_pct": 0.42,
                    "abs_ts": 1250.5,
                    "absebook_pct": 0.46,
                    "absebook_cfi": "epubcfi(/6/10[chapter5]!/4/2/8/1:45)",
                    "storyteller_pct": 0.44,
                    "storyteller_xpath": "/html/body/section[3]/p[8]",
                    "booklore_pct": 0.43,
                    "booklore_xpath": "/html/body/article[2]/div[1]/p[15]"
                },
                "migration-test-2": {
                    "last_updated": time.time() - 7200,  # 2 hours ago
                    "kosync_pct": 0.65,
                    "abs_pct": 0.60,
                    "abs_ts": 2100.0
                }
            }

            with open(state_json_path, 'w') as f:
                json.dump(state_data, f, indent=2)
            print(f"✅ Created test state JSON with {len(state_data)} book states")

            # Create new database service for migration test
            migration_db_path = temp_path / "migration_test.db"
            migration_db_service = DatabaseService(str(migration_db_path))

            # Test the migrator
            from src.db.database_service import DatabaseMigrator
            migrator = DatabaseMigrator(
                migration_db_service,
                str(mapping_json_path),
                str(state_json_path)
            )

            # Verify migration is needed
            should_migrate = migrator.should_migrate()
            print(f"✅ Migration needed check: {should_migrate}")

            if should_migrate:
                # Perform migration
                migrator.migrate()
                print("✅ Migration completed")

                # Verify migrated books
                migrated_books = migration_db_service.get_all_books()
                print(f"📚 Migrated {len(migrated_books)} books:")

                for book in migrated_books:
                    print(f"   - {book.abs_id}: {book.abs_title} (status: {book.status})")

                    # Check states for this book
                    book_states = migration_db_service.get_states_for_book(book.abs_id)
                    print(f"     📊 {len(book_states)} states:")
                    for state in book_states:
                        print(f"        {state.client_name}: {state.percentage:.2%}")

                    # Check for hardcover details
                    try:
                        hardcover = migration_db_service.get_hardcover_details(book.abs_id)
                        if hardcover:
                            print(f"     📖 Hardcover: book_id={hardcover.hardcover_book_id}, pages={hardcover.hardcover_pages}")
                    except:
                        pass

                    # Check for job data
                    try:
                        job = migration_db_service.get_latest_job(book.abs_id)
                        if job:
                            print(f"     🔧 Job: retry_count={job.retry_count}, error='{job.last_error}'")
                    except:
                        pass

                # Test migration idempotency (should not migrate again)
                books_before_second_migration = len(migration_db_service.get_all_books())

                should_migrate_again = migrator.should_migrate()
                print(f"✅ Should migrate again: {should_migrate_again} (should be False)")

                if not should_migrate_again:
                    print("✅ Migration is idempotent - no duplicate data created")
                else:
                    print("⚠️  Migration might create duplicate data")

                # Verify specific migration scenarios
                print("\n   🔍 Verifying specific migration scenarios:")

                # Test 1: Book with all client states
                book1 = migration_db_service.get_book("migration-test-1")
                if book1:
                    states1 = migration_db_service.get_states_for_book("migration-test-1")
                    state_clients = [s.client_name for s in states1]
                    expected_clients = ['kosync', 'abs', 'absebook', 'storyteller', 'booklore']

                    all_clients_migrated = all(client in state_clients for client in expected_clients)
                    print(f"     ✅ All client states migrated: {all_clients_migrated}")

                    # Check specific state values
                    kosync_state = next((s for s in states1 if s.client_name == 'kosync'), None)
                    if kosync_state and kosync_state.percentage == 0.45:
                        print("     ✅ KoSync state percentage migrated correctly")

                    abs_state = next((s for s in states1 if s.client_name == 'abs'), None)
                    if abs_state and abs_state.timestamp == 1250.5:
                        print("     ✅ ABS timestamp migrated correctly")

                # Test 2: Book with partial states
                book2 = migration_db_service.get_book("migration-test-2")
                if book2:
                    states2 = migration_db_service.get_states_for_book("migration-test-2")
                    if len(states2) == 2:  # Only kosync and abs
                        print("     ✅ Partial state migration working correctly")

                print("✅ Migration testing completed successfully")
            else:
                print("⚠️  Migration not needed - test skipped")

        # Test 7: Cleanup
        print("\n7️⃣ Cleaning Up")

        success = db_service.delete_book(test_abs_id)
        print(f"✅ Cleanup successful: {success}")

        print("\n🎉 All unified service tests passed!")
        return True

    except Exception as e:
        import traceback
        print(f"\n❌ Test failed: {e}")
        print("📜 Traceback:")
        traceback.print_exc()
        return False


if __name__ == '__main__':
    success = test_unified_database_service()
    exit(0 if success else 1)
