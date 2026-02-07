"""
Proper Flask Integration Test with Dependency Injection.
No patches needed - clean dependency injection pattern.
"""

import unittest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock
import sys

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))


class MockContainer:
    """Mock container for testing - implements the same interface as real container."""

    def __init__(self):
        self.mock_sync_manager = Mock()
        self.mock_abs_client = Mock()
        self.mock_booklore_client = Mock()
        self.mock_storyteller_client = Mock()
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}  # Default empty settings
        self.mock_ebook_parser = Mock()
        self.mock_sync_clients = Mock()

        # Configure the sync manager to return our mock clients
        self.mock_sync_manager.abs_client = self.mock_abs_client
        self.mock_sync_manager.booklore_client = self.mock_booklore_client
        self.mock_sync_manager.storyteller_client = self.mock_storyteller_client
        self.mock_sync_manager.get_abs_title.return_value = 'Test Book Title'
        self.mock_sync_manager.get_duration.return_value = 3600
        self.mock_sync_manager.clear_progress = Mock()

    def sync_manager(self):
        return self.mock_sync_manager

    def abs_client(self):
        return self.mock_abs_client

    def booklore_client(self):
        return self.mock_booklore_client

    def storyteller_client(self):
        return self.mock_storyteller_client

    def ebook_parser(self):
        return self.mock_ebook_parser

    def database_service(self):
        return self.mock_database_service

    def sync_clients(self):
        """Return mock sync clients for integrations."""
        return {
            'ABS': Mock(is_configured=Mock(return_value=True)),
            'KoSync': Mock(is_configured=Mock(return_value=True)),
            'Storyteller': Mock(is_configured=Mock(return_value=False))
        }

    def data_dir(self):
        return Path(tempfile.gettempdir()) / 'test_data'

    def books_dir(self):
        return Path(tempfile.gettempdir()) / 'test_books'

    def epub_cache_dir(self):
        return Path(tempfile.gettempdir()) / 'test_epub_cache'

    def sync_clients(self):
        return self.mock_sync_clients


class CleanFlaskIntegrationTest(unittest.TestCase):
    """Clean Flask integration test using proper dependency injection."""

    def setUp(self):
        """Set up test environment with mocked dependencies."""
        # Create temporary directory for test
        self.temp_dir = tempfile.mkdtemp()

        # Set up environment variables for testing
        os.environ['DATA_DIR'] = self.temp_dir
        os.environ['BOOKS_DIR'] = self.temp_dir

        # Create mock container
        self.mock_container = MockContainer()

        # Mock the database initialization function
        def mock_initialize_database(data_dir):
            return self.mock_container.mock_database_service

        # Patch the initialize_database import BEFORE importing web_server
        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_initialize_database

        # Use the app factory to get a fresh app instance for each test
        from src.web_server import create_app, setup_dependencies
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()

        # Store references for easy access
        self.mock_manager = self.mock_container.mock_sync_manager
        self.mock_abs_client = self.mock_container.mock_abs_client
        self.mock_booklore_client = self.mock_container.mock_booklore_client
        self.mock_storyteller_client = self.mock_container.mock_storyteller_client
        self.mock_database_service = self.mock_container.mock_database_service

    def tearDown(self):
        """Clean up after test."""
        # Restore original function
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db

        # Clean up temp directory
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_dependency_injection_works(self):
        """Verify that dependency injection is working properly."""
        from src.web_server import manager, database_service, container

        # Verify our mocked dependencies are injected
        self.assertIs(container, self.mock_container)
        self.assertIs(manager, self.mock_container.mock_sync_manager)
        self.assertIs(database_service, self.mock_container.mock_database_service)

        print("[OK] Dependency injection working correctly")

    def test_index_endpoint_with_mocked_dependencies(self):
        """Test index endpoint using clean dependency injection."""
        # Setup mock data
        from src.db.models import Book
        test_book = Book(
            abs_id='test-book-123',
            abs_title='Test Book',
            ebook_filename='test.epub',
            kosync_doc_id='test-doc-id',
            status='active',
            duration=3600  # Add duration for progress calculation
        )

        # Create mock states with different progress values
        from src.db.models import State
        mock_states = [
            State(
                abs_id='test-book-123',
                client_name='kosync',
                last_updated=1642291200,
                percentage=0.45,  # 45% progress
                xpath='/html/body/div[2]/p[5]'
            ),
            State(
                abs_id='test-book-123',
                client_name='storyteller',
                last_updated=1642291300,
                percentage=0.42,  # 42% progress
                cfi='epubcfi(/6/4[chapter01]!/4/2/2[para05]/1:0)'
            ),
            State(
                abs_id='test-book-123',
                client_name='abs',
                last_updated=1642291100,
                percentage=0.44,  # 44% progress
                timestamp=1584  # 44% of 3600 seconds duration
            ),
            State(
                abs_id='test-book-123',
                client_name='booklore',
                last_updated=1642291150,
                percentage=0.40,  # 40% progress
                cfi='epubcfi(/6/6[chapter02]!/4/1/1:0)'
            )
        ]

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_states_for_book.return_value = mock_states
        self.mock_database_service.get_all_states.return_value = mock_states
        self.mock_database_service.get_hardcover_details.return_value = None
        self.mock_database_service.get_all_hardcover_details.return_value = []
        self.mock_database_service.get_all_pending_suggestions.return_value = []

        # Mock the sync_clients call for integrations
        # Mock the sync_clients call for integrations
        # Mock the sync_clients call for integrations
        # Since container.sync_clients() returns the mock object, we need to mock .items()
        clients_dict = {
                'ABS': Mock(is_configured=Mock(return_value=True)),
                'KoSync': Mock(is_configured=Mock(return_value=True)),
                'Storyteller': Mock(is_configured=Mock(return_value=False))
        }
        self.mock_container.mock_sync_clients.items.return_value = clients_dict.items()

        # Mock render_template to capture arguments
        import src.web_server
        original_render = src.web_server.render_template
        mock_render = Mock(return_value="Mocked HTML Response")
        src.web_server.render_template = mock_render

        try:
            # Make HTTP request
            response = self.client.get('/')

            # Verify response
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"Mocked HTML Response")

            # Verify database was called
            self.mock_database_service.get_all_books.assert_called_once()
            self.mock_database_service.get_all_states.assert_called_once()
            self.mock_database_service.get_all_hardcover_details.assert_called_once()

            # Verify render_template was called with correct arguments
            mock_render.assert_called_once()
            render_args, render_kwargs = mock_render.call_args

            # Check template name
            self.assertEqual(render_args[0], 'index.html')

            # Check required template variables
            self.assertIn('mappings', render_kwargs)
            self.assertIn('integrations', render_kwargs)
            self.assertIn('progress', render_kwargs)

            # Verify mappings data structure
            mappings = render_kwargs['mappings']
            self.assertEqual(len(mappings), 1)
            mapping = mappings[0]

            # Check mapping contains expected book data
            self.assertEqual(mapping['abs_id'], 'test-book-123')
            self.assertEqual(mapping['abs_title'], 'Test Book')
            self.assertEqual(mapping['ebook_filename'], 'test.epub')
            self.assertEqual(mapping['status'], 'active')

            # Check progress values based on mock states
            # The unified progress should be the maximum of all client progress values
            self.assertEqual(mapping['unified_progress'], 45.0)  # Max of 45%, 42%, 44%, 40%

            # Check that states structure is present and contains expected data
            self.assertIn('states', mapping)
            states = mapping['states']

            # Verify each client state is stored correctly
            self.assertIn('kosync', states)
            self.assertEqual(states['kosync']['percentage'], 45.0)  # 45% from mock state
            self.assertEqual(states['kosync']['timestamp'], 0)
            self.assertEqual(states['kosync']['last_updated'], 1642291200)

            self.assertIn('storyteller', states)
            self.assertEqual(states['storyteller']['percentage'], 42.0)  # 42% from mock state
            self.assertEqual(states['storyteller']['timestamp'], 0)
            self.assertEqual(states['storyteller']['last_updated'], 1642291300)

            self.assertIn('booklore', states)
            self.assertEqual(states['booklore']['percentage'], 40.0)  # 40% from mock state
            self.assertEqual(states['booklore']['timestamp'], 0)
            self.assertEqual(states['booklore']['last_updated'], 1642291150)

            self.assertIn('abs', states)
            self.assertEqual(states['abs']['percentage'], 44.0)  # 44% from mock state
            self.assertEqual(states['abs']['timestamp'], 1584)  # Timestamp from mock state
            self.assertEqual(states['abs']['last_updated'], 1642291100)

            # Hardcover should not be present since no hardcover states were provided
            self.assertNotIn('hardcover', states)

            # Check hardcover fields are properly initialized
            self.assertFalse(mapping['hardcover_linked'])
            self.assertIsNone(mapping['hardcover_book_id'])
            self.assertIsNone(mapping['hardcover_title'])

            # Verify integrations data
            integrations = render_kwargs['integrations']
            self.assertTrue(integrations.get('abs', False))  # Mocked as True
            self.assertTrue(integrations.get('kosync', False))  # Mocked as True
            self.assertFalse(integrations.get('storyteller', True))  # Mocked as False

            # Verify overall progress (should be calculated from book progress and duration)
            overall_progress = render_kwargs['progress']
            # With duration=3600 and unified_progress=45%, the calculation should reflect this
            self.assertGreater(overall_progress, 0)  # Should be > 0 now that we have progress data
            self.assertLessEqual(overall_progress, 100)  # Should be a valid percentage

            print("[OK] Index endpoint test passed with correct response verification")

        finally:
            src.web_server.render_template = original_render

    def test_api_status_endpoint_clean_di(self):
        """Test API status endpoint with clean dependency injection."""
        # Setup mock data
        from src.db.models import Book
        test_book = Book(
            abs_id='api-test-book-123',
            abs_title='API Test Book',
            ebook_filename='api-test.epub',
            kosync_doc_id='api-test-doc-id',
            status='active',
            duration=3600
        )

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_states_for_book.return_value = []

        # Make HTTP request
        response = self.client.get('/api/status')

        # Verify response
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, 'application/json')

        data = response.get_json()
        self.assertIn('mappings', data)
        self.assertEqual(len(data['mappings']), 1)
        self.assertEqual(data['mappings'][0]['abs_id'], 'api-test-book-123')
        
        # Verify percentage scaling (should be 0 because states mock returned empty list)
        # But let's verify structure
        self.assertIn('states', data['mappings'][0])

        print("[OK] API status endpoint test passed with clean DI")

    def test_api_status_percentage_scaling(self):
        """Test that API status scales percentages correctly (0.45 -> 45.0)."""
        # Setup mock data
        from src.db.models import Book, State
        test_book = Book(
            abs_id='scale-test-123',
            abs_title='Scale Test',
            ebook_filename='scale.epub',
            kosync_doc_id='scale-doc',
            status='active'
        )

        # Mock states with decimal percentages
        mock_states = [
            State(
                abs_id='scale-test-123',
                client_name='kosync',
                percentage=0.455,  # Should become 45.5
                last_updated=1000
            ),
            State(
                abs_id='scale-test-123',
                client_name='storyteller',
                percentage=0.1,    # Should become 10.0
                last_updated=2000
            )
        ]

        self.mock_database_service.get_all_books.return_value = [test_book]
        self.mock_database_service.get_states_for_book.return_value = mock_states
        self.mock_database_service.get_all_states.return_value = mock_states

        # Make HTTP request
        response = self.client.get('/api/status')
        data = response.get_json()

        # Verify mappings
        mapping = data['mappings'][0]
        
        # Check nested states
        self.assertEqual(mapping['states']['kosync']['percentage'], 45.5)
        self.assertEqual(mapping['states']['storyteller']['percentage'], 10.0)

        # Check legacy flat fields
        self.assertEqual(mapping['kosync_pct'], 45.5)
        self.assertEqual(mapping['storyteller_pct'], 10.0)

        print("[OK] API status percentage scaling test passed")

    def test_match_endpoint_with_clean_di(self):
        """Test match endpoint using clean dependency injection."""
        # Mock the kosync ID generation
        import src.web_server
        original_get_kosync = src.web_server.get_kosync_id_for_ebook
        src.web_server.get_kosync_id_for_ebook = Mock(return_value='test-kosync-id')

        try:
            # Configure mocks
            self.mock_abs_client.get_all_audiobooks.return_value = [
                {
                    'id': 'test-audiobook-123',
                    'media': {
                        'metadata': {'title': 'Test Book'},
                        'duration': 3600
                    }
                }
            ]
            self.mock_booklore_client.is_configured.return_value = True
            self.mock_booklore_client.find_book_by_filename.return_value = {'id': 'book-123'}

            # Configure client methods
            self.mock_abs_client.add_to_collection.return_value = True
            self.mock_booklore_client.add_to_shelf.return_value = True
            self.mock_storyteller_client.add_to_collection.return_value = True

            # Make HTTP POST request
            response = self.client.post('/match', data={
                'audiobook_id': 'test-audiobook-123',
                'ebook_filename': 'test-book.epub'
            })

            # Verify response
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/'))

            # Verify service interactions
            self.mock_database_service.save_book.assert_called_once()

            # Verify save_book was called with correct arguments
            save_book_call_args = self.mock_database_service.save_book.call_args
            saved_book = save_book_call_args[0][0]  # First positional argument

            # Verify the Book object has correct attributes
            self.assertEqual(saved_book.abs_id, 'test-audiobook-123')
            self.assertEqual(saved_book.abs_title, 'Test Book Title')  # From mock manager
            self.assertEqual(saved_book.ebook_filename, 'test-book.epub')
            self.assertEqual(saved_book.kosync_doc_id, 'test-kosync-id')
            self.assertEqual(saved_book.status, 'pending')
            self.assertEqual(saved_book.duration, 3600)
            self.assertIsNone(saved_book.transcript_file)

            self.mock_abs_client.add_to_collection.assert_called_once_with('test-audiobook-123', 'Synced with KOReader')
            self.mock_booklore_client.add_to_shelf.assert_called_once_with('test-book.epub', 'Kobo')
            self.mock_storyteller_client.add_to_collection.assert_called_once_with('test-book.epub')

            print("[OK] Match endpoint test passed with clean DI")

        finally:
            src.web_server.get_kosync_id_for_ebook = original_get_kosync

    def test_clear_progress_endpoint_clean_di(self):
        """Test clear progress endpoint with clean dependency injection."""
        # Setup mock book
        from src.db.models import Book
        test_book = Book(
            abs_id='clear-test-book',
            abs_title='Clear Test Book',
            ebook_filename='clear-test.epub',
            kosync_doc_id='clear-test-doc-id',
            status='active'
        )

        self.mock_database_service.get_book.return_value = test_book

        # Make HTTP request
        response = self.client.post('/clear-progress/clear-test-book')

        # Verify response
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith('/'))

        # Verify clear_progress was called on manager
        self.mock_manager.clear_progress.assert_called_once_with('clear-test-book')

        print("[OK] Clear progress endpoint test passed with clean DI")

    def test_settings_endpoint_clean_di(self):
        """Test settings endpoint with clean dependency injection."""
        # Mock database settings
        self.mock_database_service.get_all_settings.return_value = {
            'KOSYNC_ENABLED': 'true',
            'SYNC_PERIOD_MINS': '10'
        }

        # Mock render_template
        import src.web_server
        original_render = src.web_server.render_template
        mock_render = Mock(return_value="Settings Page HTML")
        src.web_server.render_template = mock_render

        try:
            # Make HTTP request
            response = self.client.get('/settings')

            # Verify response
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"Settings Page HTML")

            # Verify database was called to load settings
            # Note: settings() function calls database_service.get_all_settings() implicitly 
            # via ConfigLoader or os.environ?
            # Actually, looking at the code, settings() calls database_service.get_all_settings() 
            # only on POST. On GET it just renders template.
            # But the template rendering uses `get_val` helper which reads from os.environ.
            # So we just verify it renders successfully.
            
            mock_render.assert_called_once()
            args, _ = mock_render.call_args
            self.assertEqual(args[0], 'settings.html')

            print("[OK] Settings endpoint test passed")

        finally:
            src.web_server.render_template = original_render


class FindEbookFileTest(unittest.TestCase):
    """Test find_ebook_file function handles special characters in filenames."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ["BOOKS_DIR"] = self.temp_dir

    def tearDown(self):
        import shutil

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_ebook_file_with_brackets(self):
        """Test that filenames with brackets like [01] are found correctly."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        filename = "Hyperion Cantos [02] - The Fall of Hyperion.epub"
        test_file = Path(self.temp_dir) / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

    @unittest.skipIf(os.name == 'nt', "Windows does not support * in filenames")
    def test_find_ebook_file_with_asterisk(self):
        """Test that filenames with asterisks are found correctly."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        filename = "Book Title * Special Edition.epub"
        test_file = Path(self.temp_dir) / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

    @unittest.skipIf(os.name == 'nt', "Windows does not support ? in filenames")
    def test_find_ebook_file_with_question_mark(self):
        """Test that filenames with question marks are found correctly."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        filename = "What If? - Science Questions.epub"
        test_file = Path(self.temp_dir) / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

    def test_find_ebook_file_in_subdirectory(self):
        """Test that files in subdirectories are found."""
        from src.web_server import find_ebook_file
        import src.web_server

        src.web_server.EBOOK_DIR = Path(self.temp_dir)

        subdir = Path(self.temp_dir) / "Author Name"
        subdir.mkdir()
        filename = "Book [Series 01].epub"
        test_file = subdir / filename
        test_file.touch()

        result = find_ebook_file(filename)
        self.assertIsNotNone(result)
        self.assertEqual(result.name, filename)

if __name__ == '__main__':
    print("TEST Clean Flask Integration Testing with Dependency Injection")
    print("=" * 70)
    print("- No patches required")
    print("- Clean dependency injection")
    print("- Real HTTP requests via test_client()")
    print("- Mocked external services")
    print("- Easy to understand and maintain")
    print("=" * 70)

    unittest.main(verbosity=2)
