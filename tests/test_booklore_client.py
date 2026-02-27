
import pytest
from unittest.mock import MagicMock, patch, mock_open
import json
import os
from pathlib import Path

# Adjust path to import src
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.booklore_client import BookloreClient
from src.db.models import BookloreBook
from src.sync_clients.sync_client_interface import LocatorResult

@pytest.fixture
def mock_db():
    return MagicMock()

@pytest.fixture
def booklore_client(mock_db):
    with patch.dict(os.environ, {
        "BOOKLORE_SERVER": "http://mock-booklore",
        "BOOKLORE_USER": "testuser",
        "BOOKLORE_PASSWORD": "testpass",
        "DATA_DIR": "/tmp/data"
    }):
        client = BookloreClient(database_service=mock_db)
        return client

def test_init_loads_from_db(mock_db):
    # Setup mock DB return
    mock_book = MagicMock()
    mock_book.filename = "test_book.epub"
    mock_book.title = "Test Book"
    mock_book.authors = "Test Author"
    mock_book.raw_metadata_dict = {
        "id": "123",
        "fileName": "test_book.epub",
        "title": "Test Book", 
        "authors": "Test Author"
    }
    
    mock_db.get_all_booklore_books.return_value = [mock_book]
    
    with patch.dict(os.environ, {"DATA_DIR": "/tmp/data"}):
        client = BookloreClient(database_service=mock_db)
        
        assert "test_book.epub" in client._book_cache
        assert client._book_cache["test_book.epub"]["id"] == "123"
        assert client._book_id_cache["123"]["title"] == "Test Book"

def test_migration_from_legacy_json(mock_db):
    # Setup: DB is empty, Legacy JSON exists
    mock_db.get_all_booklore_books.side_effect = [[], []] # First call empty, second call empty
    
    legacy_data = {
        "books": {
            "legacy.epub": {
                "id": "999",
                "title": "Legacy Book",
                "authors": "Old Author"
            }
        }
    }
    
    # Mock open AND json.load to ensure data is returned correctly
    with patch("builtins.open", mock_open(read_data=json.dumps(legacy_data))) as mock_file:
         # Need to ensure json.load reads from the mock
         with patch("json.load", return_value=legacy_data):
            with patch.object(Path, "exists", return_value=True):
                 with patch.object(Path, "rename") as mock_rename:
                    with patch.dict(os.environ, {"DATA_DIR": "/tmp/data"}):
                        client = BookloreClient(database_service=mock_db)
                        
                        # Verification
                        mock_db.save_booklore_book.assert_called_once()
                        call_args = mock_db.save_booklore_book.call_args[0][0]
                        assert isinstance(call_args, BookloreBook)
                        assert call_args.filename == "legacy.epub"
                        assert call_args.title == "Legacy Book"
                        
                        # Verify rename was called
                        mock_rename.assert_called()

def test_save_to_db_on_fetch(mock_db):
    # Setup basic client
    with patch.dict(os.environ, {
        "BOOKLORE_SERVER": "http://mock-booklore",
        "BOOKLORE_USER": "test",
        "BOOKLORE_PASSWORD": "pass",
        "DATA_DIR": "/tmp/data"
    }):
        client = BookloreClient(database_service=mock_db)
        
        # Mock dependencies
        mock_response = MagicMock()
        mock_response.status_code = 200
        # First call returns list, second empty to stop loop
        mock_response.json.side_effect = [
            [
                {
                    "id": "new1",
                    "fileName": "NewBook.epub", # Booklore sends camelCase
                    "title": "New Book",
                    "metadata": {
                        "authors": ["New Author"] # Booklore sends list of strings or dicts
                    }
                }
            ],
            [] 
        ]
        
        # Mock token and request
        client._get_fresh_token = MagicMock(return_value="fake_token")
        client._make_request = MagicMock(side_effect=[mock_response, mock_response])
        
        # Mock _fetch_book_detail to return valid detailed info
        detailed_info = {
            "id": "new1",
            "fileName": "newbook.epub", # normalized
            "title": "New Book",
            "metadata": {
                "authors": ["New Author"]
            }
        }
        
        with patch.object(client, '_fetch_book_detail', return_value=detailed_info):
            # Also mock thread pool to run synchronously or just trust the loop calls it?
            # ThreadPoolExecutor is used. mocking it or _fetch_book_detail is fine.
            # But the loop calls executor.submit(fetch_one, bid)
            # We can mock ThreadPoolExecutor too to be safe, OR just let it run since fetch_detail is mocked.
            # Since fetch_detail is mocked, it won't hit network.
            
             client._refresh_book_cache()
             
             # Verify processing happened
             # Check if save_booklore_book was called
             mock_db.save_booklore_book.assert_called()
             saved_book = mock_db.save_booklore_book.call_args[0][0]
             assert saved_book.filename == "newbook.epub"


def test_update_progress_zero_clears_cfi(booklore_client):
    booklore_client.find_book_by_filename = MagicMock(return_value={
        "id": 6043,
        "bookType": "EPUB",
        "fileName": "test-book.epub",
        "epubProgress": {"percentage": 66.3, "cfi": "epubcfi(/6/50!/:0)"},
    })
    booklore_client._book_id_cache = {
        6043: {"epubProgress": {"percentage": 66.3, "cfi": "epubcfi(/6/50!/:0)"}}
    }

    resp = MagicMock()
    resp.status_code = 200
    booklore_client._make_request = MagicMock(return_value=resp)

    ok = booklore_client.update_progress("test-book.epub", 0.0, LocatorResult(percentage=0.0))

    assert ok is True
    _, _, payload = booklore_client._make_request.call_args[0]
    assert payload["epubProgress"]["percentage"] == 0.0
    assert payload["epubProgress"]["cfi"] == ""
    assert booklore_client._book_id_cache[6043]["epubProgress"]["cfi"] == ""
