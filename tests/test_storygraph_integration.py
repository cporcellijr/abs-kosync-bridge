import unittest
from unittest.mock import MagicMock, patch
import os
import sys

# Add src to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock dependencies BEFORE import
sys.modules['selenium'] = MagicMock()
sys.modules['selenium.webdriver'] = MagicMock()
sys.modules['selenium.webdriver.common.by'] = MagicMock()
sys.modules['selenium.webdriver.support'] = MagicMock()
sys.modules['selenium.webdriver.support.ui'] = MagicMock()
sys.modules['selenium.webdriver.chrome.options'] = MagicMock()
sys.modules['selenium.webdriver.chrome.service'] = MagicMock()
sys.modules['webdriver_manager'] = MagicMock()
sys.modules['webdriver_manager.chrome'] = MagicMock()

from src.api.storygraph_client import StoryGraphClient
from src.sync_clients.storygraph_sync_client import StoryGraphSyncClient
from src.db.models import Book, StoryGraphDetails

class TestStoryGraphIntegration(unittest.TestCase):
    
    def setUp(self):
        # Mock env vars
        self.email = "test@example.com"
        self.password = "password123"
        
        # Mock dependencies
        self.mock_ebook_parser = MagicMock()
        self.mock_abs_client = MagicMock()
        self.mock_db_service = MagicMock()

    @patch('src.api.storygraph_client._check_selenium')
    @patch('src.api.storygraph_client.shutil.which')
    def test_client_configuration(self, mock_which, mock_check_selenium):
        # 1. Test is_configured
        mock_check_selenium.return_value = True
        client = StoryGraphClient(self.email, self.password)
        self.assertTrue(client.is_configured())
        
        # 2. Test missing creds
        client_empty = StoryGraphClient("", "")
        self.assertFalse(client_empty.is_configured())

        # 3. Test ensure_chromium_installed (mocking existence)
        mock_which.return_value = '/usr/bin/chromium'
        self.assertTrue(client.ensure_chromium_installed())

    @patch('src.api.storygraph_client.StoryGraphClient._create_driver')
    @patch('src.api.storygraph_client._check_selenium', return_value=True)
    def test_search_book_flow(self, mock_check, mock_create_driver):
        # Setup mock driver
        mock_driver = MagicMock()
        mock_create_driver.return_value = mock_driver
        
        client = StoryGraphClient(self.email, self.password)
        
        # Mock Login success
        # We need to mock _login internally or mock the driver interactions
        # Let's mock _login to return True to focus on search
        with patch.object(client, '_login', return_value=True):
            # Mock Search Results
            # We need to mock driver.get() and driver.find_elements()
            
            # This is complex to mock purely with Selenium mocks due to dynamic finding
            # Simpler approach: Mock _run_task to return a dummy result
            # But that defeats the purpose of testing the logic.
            
            # Low-level mock:
            mock_element.get_attribute.return_value = "https://app.thestorygraph.com/books/123-abcd"
            mock_element.text = "Test Book"
            
            # Mock finding results
            # The code calls find_elements 3 times (selectors). 
            # We make the first one return our element
            mock_driver.find_elements.return_value = [mock_element]
            
            # Mock finding author
            mock_author_elem = MagicMock()
            mock_author_elem.text = "Test Author"
            mock_driver.find_element.return_value = mock_author_elem
            
            # Mock page count (another get)
            mock_driver.page_source = "123 pages"
            
            # EXECUTE
            result = client.search_book("Test Book", "Test Author")
            
            # VERIFY
            self.assertIsNotNone(result)
            self.assertEqual(result['book_id'], "123-abcd")
            self.assertEqual(result['title'], "Test Book")
            self.assertEqual(result['pages'], 123)

    def test_sync_client_logic(self):
        # Setup
        mock_sg_client = MagicMock()
        mock_sg_client.is_configured.return_value = True
        
        sync_client = StoryGraphSyncClient(
            mock_sg_client, 
            self.mock_ebook_parser, 
            self.mock_abs_client, 
            self.mock_db_service
        )
        
        # Test 1: Should Sync Decision
        book = Book(abs_id="abs123")
        self.assertTrue(sync_client._should_sync(book, 0.5)) # First time
        
        sync_client._last_synced_progress["abs123"] = 0.5
        self.assertFalse(sync_client._should_sync(book, 0.505)) # < 1% diff
        self.assertTrue(sync_client._should_sync(book, 0.52)) # > 1% diff
        
        # Test 2: Automatch Logic (Success)
        self.mock_db_service.get_storygraph_details.return_value = None
        self.mock_abs_client.get_item_details.return_value = {
            'media': {'metadata': {'title': 'Dune', 'authorName': 'Frank Herbert'}}
        }
        mock_sg_client.search_book.return_value = {
            'book_id': 'dune-123', 'title': 'Dune', 'pages': 500
        }
        
        details = sync_client._automatch_storygraph(book)
        
        self.assertIsNotNone(details)
        self.assertEqual(details.storygraph_id, 'dune-123')
        self.mock_db_service.save_storygraph_details.assert_called()

if __name__ == '__main__':
    unittest.main()
