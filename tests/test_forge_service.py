
import unittest
from unittest.mock import MagicMock, patch, ANY
import sys
from pathlib import Path
import threading
import time

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.services.forge_service import ForgeService

class TestForgeService(unittest.TestCase):
    def setUp(self):
        self.mock_db = MagicMock()
        self.mock_abs = MagicMock()
        self.mock_booklore = MagicMock()
        self.mock_storyteller = MagicMock()
        self.mock_library = MagicMock()
        self.mock_ebook_parser = MagicMock()
        
        self.service = ForgeService(
            database_service=self.mock_db,
            abs_client=self.mock_abs,
            booklore_client=self.mock_booklore,
            storyteller_client=self.mock_storyteller,
            library_service=self.mock_library,
            ebook_parser=self.mock_ebook_parser
        )
        
        # Suppress logging during tests
        self.logger_patch = patch('src.services.forge_service.logger')
        self.logger_patch.start()

    def tearDown(self):
        patch.stopall()

    def test_start_manual_forge(self):
        """Test starting a manual forge process."""
        # We process start_manual_forge which creates a thread targeting _forge_background_task
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance
            
            self.service.start_manual_forge(
                abs_id="abs456",
                text_item={"path": "other.epub"},
                title="Test Book 2",
                author="Test Author 2"
            )
            
            mock_thread_cls.assert_called_with(
                target=self.service._forge_background_task,
                args=("abs456", {"path": "other.epub"}, "Test Book 2", "Test Author 2"),
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()


    def test_start_auto_forge_match(self):
        """Test starting auto forge match."""
        # Using mock threading
        with patch('threading.Thread') as mock_thread_cls:
            mock_thread_instance = MagicMock()
            mock_thread_cls.return_value = mock_thread_instance
            
            self.service.start_auto_forge_match(
                abs_id="abs789",
                text_item={"booklore_id": 1},
                title="Auto Book",
                author="Auto Author",
                original_filename="orig.epub",
                original_hash="hash123"
            )
            
            mock_thread_cls.assert_called_with(
                target=self.service._auto_forge_background_task,
                args=("abs789", {"booklore_id": 1}, "Auto Book", "Auto Author", "orig.epub", "hash123"),
                daemon=True
            )
            mock_thread_instance.start.assert_called_once()

if __name__ == '__main__':
    unittest.main()
