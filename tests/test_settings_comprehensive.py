
import unittest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch
import sys

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

class MockContainer:
    """Mock container for testing."""
    def __init__(self):
        self.mock_database_service = Mock()
        self.mock_database_service.get_all_settings.return_value = {}
        self.mock_sync_manager = Mock()
        self.mock_sync_manager.get_abs_title.return_value = 'Test'
        
    def database_service(self): return self.mock_database_service
    def sync_manager(self): return self.mock_sync_manager
    def abs_client(self): return Mock()
    def booklore_client(self): return Mock()
    def storyteller_client(self): return Mock()
    def hardcover_client(self): return Mock()
    def transcriber(self): return Mock()
    def ebook_parser(self): return Mock()
    def sync_clients(self): return {}
    def data_dir(self): return Path(tempfile.gettempdir())
    def books_dir(self): return Path(tempfile.gettempdir())
    def epub_cache_dir(self): return Path(tempfile.gettempdir()) / 'test_epub_cache'

class TestSettingsComprehensive(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        os.environ['DATA_DIR'] = self.temp_dir
        
        self.mock_container = MockContainer()
        
        # Mock database initialization
        def mock_init_db(data_dir):
            return self.mock_container.mock_database_service
            
        import src.db.migration_utils
        self.original_init_db = src.db.migration_utils.initialize_database
        src.db.migration_utils.initialize_database = mock_init_db

        from src.web_server import create_app
        self.app, _ = create_app(test_container=self.mock_container)
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

        # List of all boolean keys from web_server.py
        self.bool_keys = [
            'KOSYNC_USE_PERCENTAGE_FROM_SERVER',
            'SYNC_ABS_EBOOK',
            'XPATH_FALLBACK_TO_PREVIOUS_SEGMENT',
            'KOSYNC_ENABLED',
            'STORYTELLER_ENABLED',
            'BOOKLORE_ENABLED',
            'HARDCOVER_ENABLED',
            'TELEGRAM_ENABLED',
            'SUGGESTIONS_ENABLED'
        ]

    def tearDown(self):
        import src.db.migration_utils
        src.db.migration_utils.initialize_database = self.original_init_db
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # Clear env vars
        for key in self.bool_keys:
            if key in os.environ:
                del os.environ[key]

    @patch('src.web_server.restart_server')
    def test_all_bool_toggles(self, mock_restart):
        """Verify EVERY boolean setting can be toggled ON and OFF."""
        
        # 1. Turn EVERYTHING ON
        # Construct form data with all keys present (simulating checked checkboxes)
        data_on = {key: 'on' for key in self.bool_keys}
        # Add a required non-bool field so validation passes if any
        data_on['SYNC_PERIOD_MINS'] = '5'
        
        self.client.post('/settings', data=data_on)
        
        # Verify calls to set_setting with 'true'
        for key in self.bool_keys:
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, 'true')
            self.assertEqual(os.environ.get(key), 'true', f"{key} should be 'true' in env")

        # Reset mock calls for clean check
        self.mock_container.mock_database_service.reset_mock()

        # 2. Turn EVERYTHING OFF
        # Construct form data with NONE of the keys (simulating unchecked checkboxes)
        data_off = {
            'SYNC_PERIOD_MINS': '5'
        }
        
        self.client.post('/settings', data=data_off)
        
        # Verify calls to set_setting with 'false'
        for key in self.bool_keys:
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, 'false')
            self.assertEqual(os.environ.get(key), 'false', f"{key} should be 'false' in env")

    @patch('src.web_server.restart_server')
    def test_text_fields_save(self, mock_restart):
        """Verify text fields correspond to logic."""
        test_data = {
            'TZ': 'Europe/Paris',
            'SYNC_PERIOD_MINS': '15',
            'ABS_SERVER': 'http://test.com'
        }
        
        self.client.post('/settings', data=test_data)
        
        for key, val in test_data.items():
            self.mock_container.mock_database_service.set_setting.assert_any_call(key, val)

if __name__ == '__main__':
    unittest.main()
