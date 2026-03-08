
import pytest
from unittest.mock import MagicMock, patch, mock_open
import json
import os
import time
from pathlib import Path

# Adjust path to import src
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.booklore_client import (
    BookloreClient,
    BULK_DETAIL_FETCH_LIMIT,
    MAX_DETAIL_FETCHES_PER_SEARCH,
)
from src.db.models import BookloreBook
from src.sync_clients.sync_client_interface import LocatorResult

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_all_booklore_books.return_value = []
    return db

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


class MockResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def make_list_book(book_id, title=None, library_id="lib-1", library_name="Library 1"):
    return {
        "id": book_id,
        "title": title or f"Book {book_id}",
        "libraryId": library_id,
        "libraryName": library_name,
    }


def make_detail(book_id, title=None, filename=None, library_id="lib-1", authors=None):
    safe_title = title or f"Book {book_id}"
    safe_filename = filename or f"{safe_title.lower().replace(' ', '-')}.epub"
    return {
        "id": book_id,
        "libraryId": library_id,
        "title": safe_title,
        "primaryFile": {
            "fileName": safe_filename,
            "filePath": f"/books/{safe_filename}",
            "bookType": "EPUB",
        },
        "metadata": {
            "title": safe_title,
            "authors": authors or ["Author"],
        },
    }


def paginated_responses(books, batch_size=200):
    responses = []
    for start in range(0, len(books), batch_size):
        responses.append(MockResponse({"content": books[start:start + batch_size]}))
    if not responses or len(books) % batch_size == 0:
        responses.append(MockResponse({"content": []}))
    return responses

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


def test_get_fresh_token_retries_duplicate_refresh_token_conflict(booklore_client):
    conflict_response = MagicMock()
    conflict_response.status_code = 400
    conflict_response.text = (
        "Duplicate entry 'abc' for key 'uq_refresh_token'"
    )

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.json.return_value = {"accessToken": "token-123"}

    booklore_client.session.post = MagicMock(
        side_effect=[conflict_response, success_response]
    )

    with patch("time.sleep") as mock_sleep:
        token = booklore_client._get_fresh_token()

    assert token == "token-123"
    assert booklore_client.session.post.call_count == 2
    mock_sleep.assert_called_once_with(booklore_client._token_login_retry_delay)


def test_get_fresh_token_skips_login_when_cached_token_is_fresh(booklore_client):
    booklore_client._token = "cached-token"
    booklore_client._token_timestamp = time.time()
    booklore_client.session.post = MagicMock()

    token = booklore_client._get_fresh_token()

    assert token == "cached-token"
    booklore_client.session.post.assert_not_called()


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

    post_resp = MagicMock()
    post_resp.status_code = 200

    verify_resp = MagicMock()
    verify_resp.status_code = 200
    verify_resp.json.return_value = {
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {"percentage": 0.0, "cfi": ""},
    }

    booklore_client._make_request = MagicMock(side_effect=[post_resp, verify_resp])

    ok = booklore_client.update_progress("test-book.epub", 0.0, LocatorResult(percentage=0.0))

    assert ok is True
    _, _, payload = booklore_client._make_request.call_args_list[0][0]
    assert payload["epubProgress"]["percentage"] == 0.0
    assert payload["epubProgress"]["cfi"] is None
    assert booklore_client._book_id_cache[6043]["epubProgress"]["cfi"] == ""


def test_update_progress_zero_retries_clear_variants_until_verified(booklore_client):
    booklore_client.find_book_by_filename = MagicMock(return_value={
        "id": 6043,
        "bookType": "EPUB",
        "fileName": "test-book.epub",
        "epubProgress": {"percentage": 66.3, "cfi": "epubcfi(/6/50!/:0)"},
    })
    booklore_client._book_id_cache = {
        6043: {"epubProgress": {"percentage": 66.3, "cfi": "epubcfi(/6/50!/:0)"}}
    }

    post1 = MagicMock()
    post1.status_code = 200
    verify1 = MagicMock()
    verify1.status_code = 200
    verify1.json.return_value = {
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {"percentage": 66.3, "cfi": ""},
    }

    post2 = MagicMock()
    post2.status_code = 200
    verify2 = MagicMock()
    verify2.status_code = 200
    verify2.json.return_value = {
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {"percentage": 0.0, "cfi": None},
    }

    booklore_client._make_request = MagicMock(side_effect=[post1, verify1, post2, verify2])

    ok = booklore_client.update_progress("test-book.epub", 0.0, LocatorResult(percentage=0.0))

    assert ok is True
    assert booklore_client._make_request.call_count == 4

    first_post = booklore_client._make_request.call_args_list[0][0]
    second_post = booklore_client._make_request.call_args_list[2][0]

    assert first_post[0] == "POST"
    assert first_post[1] == "/api/v1/books/progress"
    assert first_post[2]["epubProgress"]["cfi"] is None

    assert second_post[0] == "POST"
    assert second_post[1] == "/api/v1/books/progress"
    assert "cfi" not in second_post[2]["epubProgress"]


def test_update_progress_retries_without_cfi_if_verified_pct_mismatch(booklore_client):
    booklore_client.find_book_by_filename = MagicMock(return_value={
        "id": 7084,
        "bookType": "EPUB",
        "fileName": "test-book.epub",
    })
    booklore_client._book_id_cache = {7084: {"epubProgress": {"percentage": 7.0, "cfi": ""}}}

    post1 = MagicMock()
    post1.status_code = 200
    verify1 = MagicMock()
    verify1.status_code = 200
    verify1.json.return_value = {
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {"percentage": 7.0, "cfi": "epubcfi(/6/4!/4/4,/58/1:259,/72/1:23)"},
    }

    post2 = MagicMock()
    post2.status_code = 200
    verify2 = MagicMock()
    verify2.status_code = 200
    verify2.json.return_value = {
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {"percentage": 14.3, "cfi": "epubcfi(/6/4!/4/4/208:0)"},
    }

    booklore_client._make_request = MagicMock(side_effect=[post1, verify1, post2, verify2])

    ok = booklore_client.update_progress(
        "test-book.epub",
        0.143,
        LocatorResult(percentage=0.143, cfi="epubcfi(/6/4!/4/4/208:0)")
    )

    assert ok is True
    assert booklore_client._make_request.call_count == 4

    first_post = booklore_client._make_request.call_args_list[0][0]
    second_post = booklore_client._make_request.call_args_list[2][0]
    assert first_post[2]["epubProgress"]["cfi"] == "epubcfi(/6/4!/4/4/208:0)"
    assert "cfi" not in second_post[2]["epubProgress"]


def test_update_progress_hydrates_lightweight_entry_when_book_type_missing(booklore_client):
    lightweight = {
        "id": 6043,
        "fileName": "test-book.epub",
        "_needs_detail": True,
    }
    hydrated = {
        "id": 6043,
        "bookType": "EPUB",
        "fileName": "test-book.epub",
        "epubProgress": {"percentage": 12.0, "cfi": ""},
    }
    booklore_client.find_book_by_filename = MagicMock(return_value=lightweight)
    booklore_client._fetch_and_cache_detail = MagicMock(return_value=hydrated)

    post_resp = MagicMock()
    post_resp.status_code = 200
    verify_resp = MagicMock()
    verify_resp.status_code = 200
    verify_resp.json.return_value = {
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {"percentage": 50.0, "cfi": ""},
    }
    booklore_client._make_request = MagicMock(side_effect=[post_resp, verify_resp])

    ok = booklore_client.update_progress("test-book.epub", 0.5, LocatorResult(percentage=0.5))

    assert ok is True
    booklore_client._fetch_and_cache_detail.assert_called_once_with(6043)
    _, _, payload = booklore_client._make_request.call_args_list[0][0]
    assert payload["epubProgress"]["percentage"] == 50.0


def test_update_progress_infers_book_type_from_filename_when_missing(booklore_client):
    booklore_client.find_book_by_filename = MagicMock(return_value={
        "id": 6043,
        "fileName": "test-book.epub",
    })
    booklore_client._fetch_and_cache_detail = MagicMock()

    post_resp = MagicMock()
    post_resp.status_code = 200
    verify_resp = MagicMock()
    verify_resp.status_code = 200
    verify_resp.json.return_value = {
        "primaryFile": {"bookType": "EPUB"},
        "epubProgress": {"percentage": 40.0, "cfi": ""},
    }
    booklore_client._make_request = MagicMock(side_effect=[post_resp, verify_resp])

    ok = booklore_client.update_progress("test-book.epub", 0.4, LocatorResult(percentage=0.4))

    assert ok is True
    booklore_client._fetch_and_cache_detail.assert_not_called()
    _, _, payload = booklore_client._make_request.call_args_list[0][0]
    assert "epubProgress" in payload


def test_update_progress_404_evicts_stale_hydrated_entry(booklore_client):
    booklore_client._process_book_detail(make_detail("gone", title="Gone Book", filename="gone.epub"))
    booklore_client.db.delete_booklore_book.reset_mock()

    response = MagicMock()
    response.status_code = 404
    booklore_client._make_request = MagicMock(return_value=response)

    ok = booklore_client.update_progress("gone.epub", 0.5, LocatorResult(percentage=0.5))

    assert ok is False
    assert "gone" not in booklore_client._book_id_cache
    assert "gone.epub" not in booklore_client._book_cache
    booklore_client.db.delete_booklore_book.assert_called_once_with("gone.epub")


def test_search_books_miss_triggers_single_refresh_and_returns_new_match(booklore_client):
    booklore_client._book_cache = {
        "old.epub": {"fileName": "old.epub", "title": "Old Book", "authors": "Old Author"}
    }
    booklore_client._cache_timestamp = time.time() - 120
    booklore_client._is_refresh_on_cooldown = MagicMock(return_value=False)

    def refresh_side_effect(**kwargs):
        booklore_client._book_cache["new-book.epub"] = {
            "fileName": "new-book.epub",
            "title": "New Arrival",
            "authors": "New Author",
        }
        booklore_client._cache_timestamp = time.time()
        return True

    booklore_client._refresh_book_cache = MagicMock(side_effect=refresh_side_effect)

    results = booklore_client.search_books("new arrival")

    assert len(results) == 1
    assert results[0]["fileName"] == "new-book.epub"
    booklore_client._refresh_book_cache.assert_called_once()


def test_search_books_miss_skips_refresh_when_cache_is_fresh(booklore_client):
    booklore_client._book_cache = {
        "old.epub": {"fileName": "old.epub", "title": "Old Book", "authors": "Old Author"}
    }
    booklore_client._cache_timestamp = time.time() - 10
    booklore_client._is_refresh_on_cooldown = MagicMock(return_value=False)
    booklore_client._refresh_book_cache = MagicMock(return_value=True)

    results = booklore_client.search_books("new arrival")

    assert results == []
    booklore_client._refresh_book_cache.assert_not_called()


def test_search_books_miss_respects_cooldown(booklore_client):
    booklore_client._book_cache = {
        "old.epub": {"fileName": "old.epub", "title": "Old Book", "authors": "Old Author"}
    }
    booklore_client._cache_timestamp = time.time() - 120
    booklore_client._is_refresh_on_cooldown = MagicMock(return_value=True)
    booklore_client._refresh_book_cache = MagicMock(return_value=True)

    results = booklore_client.search_books("new arrival")

    assert results == []
    booklore_client._refresh_book_cache.assert_not_called()


def test_search_books_miss_refresh_failure_returns_empty_without_retry_loop(booklore_client):
    booklore_client._book_cache = {
        "old.epub": {"fileName": "old.epub", "title": "Old Book", "authors": "Old Author"}
    }
    booklore_client._cache_timestamp = time.time() - 120
    booklore_client._is_refresh_on_cooldown = MagicMock(return_value=False)
    booklore_client._refresh_book_cache = MagicMock(return_value=False)

    results = booklore_client.search_books("new arrival")

    assert results == []
    booklore_client._refresh_book_cache.assert_called_once()


def test_search_books_hit_triggers_single_refresh_and_prunes_deleted_result_when_cache_old(booklore_client):
    booklore_client._book_cache = {
        "deleted.epub": {"id": "deleted", "fileName": "deleted.epub", "title": "Deleted Book", "authors": "Old Author"}
    }
    booklore_client._book_id_cache = {
        "deleted": {"id": "deleted", "fileName": "deleted.epub", "title": "Deleted Book", "authors": "Old Author"}
    }
    booklore_client._cache_timestamp = time.time() - 1900
    booklore_client._search_hit_refresh_min_age = 60
    booklore_client._is_refresh_on_cooldown = MagicMock(return_value=False)

    def refresh_side_effect(**kwargs):
        booklore_client._book_cache.clear()
        booklore_client._book_id_cache.clear()
        booklore_client._cache_timestamp = time.time()
        return True

    booklore_client._refresh_book_cache = MagicMock(side_effect=refresh_side_effect)

    results = booklore_client.search_books("deleted")

    assert results == []
    booklore_client._refresh_book_cache.assert_called_once()


def test_search_books_hit_skips_refresh_when_cache_is_fresh(booklore_client):
    booklore_client._book_cache = {
        "deleted.epub": {"id": "deleted", "fileName": "deleted.epub", "title": "Deleted Book", "authors": "Old Author"}
    }
    booklore_client._book_id_cache = {
        "deleted": {"id": "deleted", "fileName": "deleted.epub", "title": "Deleted Book", "authors": "Old Author"}
    }
    booklore_client._cache_timestamp = time.time() - 10
    booklore_client._is_refresh_on_cooldown = MagicMock(return_value=False)
    booklore_client._refresh_book_cache = MagicMock(return_value=True)

    results = booklore_client.search_books("deleted")

    assert len(results) == 1
    assert results[0]["fileName"] == "deleted.epub"
    booklore_client._refresh_book_cache.assert_not_called()


def test_refresh_book_cache_hydrates_small_library(booklore_client):
    books = [make_list_book(f"book-{idx}", title=f"Small Book {idx}") for idx in range(3)]
    booklore_client._make_request = MagicMock(side_effect=paginated_responses(books))
    booklore_client._get_fresh_token = MagicMock(return_value="token")
    booklore_client._fetch_book_detail = MagicMock(
        side_effect=lambda book_id, token: make_detail(
            book_id,
            title=f"Small Book {book_id.split('-')[-1]}",
            filename=f"small-book-{book_id.split('-')[-1]}.epub",
        )
    )

    assert booklore_client._refresh_book_cache(refresh_stale_details=False) is True
    assert booklore_client._fetch_book_detail.call_count == 3
    assert len(booklore_client._book_cache) == 3
    assert len(booklore_client._book_id_cache) == 3
    assert all(not info.get('_needs_detail') for info in booklore_client._book_id_cache.values())
    assert booklore_client.db.save_booklore_book.call_count == 3


def test_refresh_book_cache_skips_bulk_detail_fetch_for_large_library(booklore_client):
    books = [
        make_list_book(f"book-{idx}", title=f"Large Book {idx}")
        for idx in range(BULK_DETAIL_FETCH_LIMIT + 1)
    ]
    booklore_client._make_request = MagicMock(side_effect=paginated_responses(books))
    booklore_client._get_fresh_token = MagicMock(return_value="token")
    booklore_client._fetch_book_detail = MagicMock()

    assert booklore_client._refresh_book_cache(refresh_stale_details=False) is True
    assert booklore_client._fetch_book_detail.call_count == 0
    assert len(booklore_client._book_cache) == 0
    assert len(booklore_client._book_id_cache) == len(books)
    assert all(info.get('_needs_detail') for info in booklore_client._book_id_cache.values())
    booklore_client.db.save_booklore_book.assert_not_called()


def test_search_books_hydrates_lightweight_entry_once(booklore_client):
    booklore_client._book_id_cache = {
        "hail-mary": {
            "id": "hail-mary",
            "title": "Project Hail Mary",
            "authors": "",
            "fileName": None,
            "libraryId": "lib-1",
            "_needs_detail": True,
        }
    }
    booklore_client._cache_timestamp = time.time()
    booklore_client._get_fresh_token = MagicMock(return_value="token")
    booklore_client._fetch_book_detail = MagicMock(
        side_effect=lambda book_id, token: make_detail(
            book_id,
            title="Project Hail Mary",
            filename="project-hail-mary.epub",
        )
    )

    first_results = booklore_client.search_books("Hail Mary")
    second_results = booklore_client.search_books("Hail Mary")
    missing_results = booklore_client.search_books("Does Not Exist")

    assert [book["fileName"] for book in first_results] == ["project-hail-mary.epub"]
    assert [book["fileName"] for book in second_results] == ["project-hail-mary.epub"]
    assert missing_results == []
    assert booklore_client._fetch_book_detail.call_count == 1


def test_search_books_caps_detail_fetches_for_broad_lightweight_search(booklore_client):
    booklore_client._book_id_cache = {
        f"the-{idx}": {
            "id": f"the-{idx}",
            "title": f"The Broad Match {idx}",
            "authors": "",
            "fileName": None,
            "libraryId": "lib-1",
            "_needs_detail": True,
        }
        for idx in range(MAX_DETAIL_FETCHES_PER_SEARCH + 5)
    }
    booklore_client._cache_timestamp = time.time()
    booklore_client._get_fresh_token = MagicMock(return_value="token")
    booklore_client._fetch_book_detail = MagicMock(
        side_effect=lambda book_id, token: make_detail(
            book_id,
            title=booklore_client._book_id_cache[book_id]["title"],
            filename=f"{book_id}.epub",
        )
    )

    results = booklore_client.search_books("The")

    assert len(results) == MAX_DETAIL_FETCHES_PER_SEARCH
    assert booklore_client._fetch_book_detail.call_count == MAX_DETAIL_FETCHES_PER_SEARCH
    assert len(booklore_client._book_cache) == MAX_DETAIL_FETCHES_PER_SEARCH


def test_get_all_books_returns_mixed_hydrated_and_lightweight_entries(booklore_client):
    booklore_client._process_book_detail(make_detail("hydrated", title="Hydrated Book", filename="hydrated.epub"))
    booklore_client._book_id_cache["lightweight"] = {
        "id": "lightweight",
        "title": "Lightweight Book",
        "authors": "",
        "fileName": None,
        "libraryId": "lib-1",
        "_needs_detail": True,
    }
    booklore_client._cache_timestamp = time.time()
    booklore_client._refresh_book_cache = MagicMock(return_value=True)

    books = booklore_client.get_all_books()

    assert len(books) == 2
    assert any(book.get("fileName") == "hydrated.epub" for book in books)
    assert any(book.get("_needs_detail") for book in books)
    booklore_client._refresh_book_cache.assert_not_called()


def test_lightweight_cache_does_not_force_refresh_on_every_read(booklore_client):
    booklore_client._book_id_cache = {
        "book-1": {
            "id": "book-1",
            "title": "Lightweight Book",
            "authors": "",
            "fileName": None,
            "libraryId": "lib-1",
            "_needs_detail": True,
        }
    }
    booklore_client._book_cache = {}
    booklore_client._cache_timestamp = time.time()
    booklore_client._refresh_book_cache = MagicMock(return_value=True)
    booklore_client._fetch_and_cache_detail = MagicMock(return_value=None)

    assert len(booklore_client.get_all_books()) == 1
    assert booklore_client.search_books("missing") == []
    assert booklore_client.find_book_by_filename("missing.epub") is None
    assert booklore_client._refresh_book_cache.call_count == 0


def test_refresh_book_cache_prunes_stale_entries_from_both_caches(booklore_client):
    hydrated_detail = make_detail("keep", title="Keep Me", filename="keep.epub")
    booklore_client._process_book_detail(hydrated_detail)
    booklore_client._book_id_cache["stale-light"] = {
        "id": "stale-light",
        "title": "Stale Lightweight",
        "authors": "",
        "fileName": None,
        "libraryId": "lib-1",
        "_needs_detail": True,
    }
    booklore_client._process_book_detail(make_detail("stale-full", title="Stale Full", filename="stale-full.epub"))
    booklore_client.db.delete_booklore_book.reset_mock()

    booklore_client._make_request = MagicMock(side_effect=paginated_responses([make_list_book("keep", title="Keep Me")]))
    booklore_client._get_fresh_token = MagicMock(return_value="token")
    booklore_client._fetch_book_detail = MagicMock()

    assert booklore_client._refresh_book_cache() is True
    assert set(booklore_client._book_id_cache.keys()) == {"keep"}
    assert set(booklore_client._book_cache.keys()) == {"keep.epub"}
    booklore_client.db.delete_booklore_book.assert_called_once_with("stale-full.epub")


def test_fetch_book_detail_404_evicts_lightweight_cache_entry(booklore_client):
    booklore_client._book_id_cache["stale-light"] = {
        "id": "stale-light",
        "title": "Stale Lightweight",
        "authors": "",
        "fileName": None,
        "_needs_detail": True,
    }

    response = MagicMock()
    response.status_code = 404

    with patch("src.api.booklore_client.requests.get", return_value=response):
        detail = booklore_client._fetch_book_detail("stale-light", "token")

    assert detail is None
    assert "stale-light" not in booklore_client._book_id_cache


def test_download_book_404_evicts_stale_hydrated_entry(booklore_client):
    booklore_client._process_book_detail(make_detail("gone", title="Gone Book", filename="gone.epub"))
    booklore_client.db.delete_booklore_book.reset_mock()
    booklore_client._get_fresh_token = MagicMock(return_value="token")

    first_response = MagicMock()
    first_response.status_code = 404
    second_response = MagicMock()
    second_response.status_code = 404
    booklore_client.session.get = MagicMock(side_effect=[first_response, second_response])

    content = booklore_client.download_book("gone")

    assert content is None
    assert "gone" not in booklore_client._book_id_cache
    assert "gone.epub" not in booklore_client._book_cache
    booklore_client.db.delete_booklore_book.assert_called_once_with("gone.epub")


def test_get_progress_404_evicts_stale_hydrated_entry(booklore_client):
    booklore_client._process_book_detail(make_detail("gone", title="Gone Book", filename="gone.epub"))
    booklore_client.db.delete_booklore_book.reset_mock()
    response = MagicMock()
    response.status_code = 404
    booklore_client._make_request = MagicMock(return_value=response)

    progress = booklore_client._get_progress_by_book_id("gone")

    assert progress == (None, None)
    assert "gone" not in booklore_client._book_id_cache
    assert "gone.epub" not in booklore_client._book_cache
    booklore_client.db.delete_booklore_book.assert_called_once_with("gone.epub")


def test_add_to_shelf_404_evicts_stale_hydrated_entry(booklore_client):
    booklore_client._process_book_detail(make_detail("gone", title="Gone Book", filename="gone.epub"))
    booklore_client.db.delete_booklore_book.reset_mock()

    shelves_response = MagicMock()
    shelves_response.status_code = 200
    shelves_response.json.return_value = [{"id": "shelf-1", "name": "Kobo"}]
    assign_response = MagicMock()
    assign_response.status_code = 404
    booklore_client._make_request = MagicMock(side_effect=[shelves_response, assign_response])

    ok = booklore_client.add_to_shelf("gone.epub", shelf_name="Kobo")

    assert ok is False
    assert "gone" not in booklore_client._book_id_cache
    assert "gone.epub" not in booklore_client._book_cache
    booklore_client.db.delete_booklore_book.assert_called_once_with("gone.epub")


def test_refresh_book_cache_uses_server_side_library_filter_when_supported(mock_db):
    with patch.dict(os.environ, {
        "BOOKLORE_SERVER": "http://mock-booklore",
        "BOOKLORE_USER": "testuser",
        "BOOKLORE_PASSWORD": "testpass",
        "BOOKLORE_LIBRARY_ID": "target-lib",
        "DATA_DIR": "/tmp/data"
    }):
        client = BookloreClient(database_service=mock_db)

    books = [make_list_book("filtered-1", title="Filtered Book", library_id="target-lib")]
    client._make_request = MagicMock(side_effect=[MockResponse(books)])
    client._get_fresh_token = MagicMock(return_value="token")
    client._fetch_book_detail = MagicMock(
        return_value=make_detail("filtered-1", title="Filtered Book", filename="filtered-book.epub", library_id="target-lib")
    )

    assert client._refresh_book_cache() is True
    first_endpoint = client._make_request.call_args_list[0][0][1]
    assert first_endpoint == "/api/v1/libraries/target-lib/book"
    assert client._make_request.call_count == 1
    assert client._server_side_filter_supported is True
    assert list(client._book_cache.keys()) == ["filtered-book.epub"]


def test_refresh_book_cache_falls_back_when_server_side_library_filter_is_ignored(mock_db):
    with patch.dict(os.environ, {
        "BOOKLORE_SERVER": "http://mock-booklore",
        "BOOKLORE_USER": "testuser",
        "BOOKLORE_PASSWORD": "testpass",
        "BOOKLORE_LIBRARY_ID": "target-lib",
        "DATA_DIR": "/tmp/data"
    }):
        client = BookloreClient(database_service=mock_db)

    mixed_page = [
        make_list_book("target-1", title="Target Book", library_id="target-lib"),
        make_list_book("other-1", title="Other Book", library_id="other-lib"),
    ]
    client._make_request = MagicMock(side_effect=[MockResponse(mixed_page), MockResponse({"content": mixed_page})])
    client._get_fresh_token = MagicMock(return_value="token")
    client._fetch_book_detail = MagicMock(
        return_value=make_detail("target-1", title="Target Book", filename="target-book.epub", library_id="target-lib")
    )

    assert client._refresh_book_cache() is True
    first_endpoint = client._make_request.call_args_list[0][0][1]
    second_endpoint = client._make_request.call_args_list[1][0][1]
    assert first_endpoint == "/api/v1/libraries/target-lib/book"
    assert second_endpoint == "/api/v1/books?page=0&size=200"
    assert client._server_side_filter_supported is False
    assert list(client._book_cache.keys()) == ["target-book.epub"]


def test_upsert_lightweight_entry_preserves_nested_summary_fields(booklore_client):
    booklore_client._upsert_lightweight_entry({
        "id": "bl-1",
        "libraryId": "lib-1",
        "libraryName": "Main Library",
        "metadata": {
            "title": "Fever Dream",
            "subtitle": "A Novel",
            "authors": [{"name": "Samanta Schweblin"}],
        },
        "primaryFile": {
            "fileName": "Fever Dream - Samanta Schweblin (2016).epub",
        },
    })

    cached = booklore_client._book_id_cache["bl-1"]
    assert cached["title"] == "Fever Dream"
    assert cached["subtitle"] == "A Novel"
    assert cached["authors"] == "Samanta Schweblin"
    assert cached["fileName"] == "Fever Dream - Samanta Schweblin (2016).epub"
    assert booklore_client._book_cache["fever dream - samanta schweblin (2016).epub"]["id"] == "bl-1"


def test_search_books_finds_lightweight_entries_without_detail_fetch(booklore_client):
    booklore_client._upsert_lightweight_entry({
        "id": "bl-1",
        "libraryId": "lib-1",
        "libraryName": "Main Library",
        "metadata": {
            "title": "Fever Dream",
            "authors": [{"name": "Samanta Schweblin"}],
        },
        "primaryFile": {
            "fileName": "Fever Dream - Samanta Schweblin (2016).epub",
        },
    })
    booklore_client._fetch_and_cache_detail = MagicMock()
    booklore_client._cache_timestamp = time.time()

    results = booklore_client.search_books("fever")

    assert len(results) == 1
    assert results[0]["id"] == "bl-1"
    assert results[0]["fileName"] == "Fever Dream - Samanta Schweblin (2016).epub"
    booklore_client._fetch_and_cache_detail.assert_not_called()


def test_search_audiobooks_includes_combined_book_using_alternative_formats(booklore_client):
    combined_detail = {
        "id": 6798,
        "libraryId": "lib-1",
        "metadata": {
            "title": "The Mars Anomaly",
            "authors": ["Joshua T. Calvert"],
            "audiobookMetadata": {
                "durationSeconds": 33945,
                "chapterCount": 50,
            },
        },
        "primaryFile": {
            "fileName": "The Mars Anomaly - Joshua T. Calvert (2024).epub",
            "filePath": "/books/The Mars Anomaly - Joshua T. Calvert (2024).epub",
            "bookType": "EPUB",
            "id": 7605,
        },
        "alternativeFormats": [
            {
                "id": 10157,
                "bookType": "AUDIOBOOK",
                "fileName": "The Mars Anomaly - Joshua T. Calvert (2024).m4b",
            }
        ],
        "supplementaryFiles": [],
        "audiobookProgress": None,
        "epubProgress": None,
    }
    booklore_client._process_book_detail(combined_detail)
    booklore_client._cache_timestamp = time.time()
    booklore_client.get_audiobook_info = MagicMock(return_value={"bookFileId": 10157, "durationMs": 33945000})

    results = booklore_client.search_audiobooks("Mars Anomaly")

    assert len(results) == 1
    assert results[0]["id"] == 6798
    assert results[0]["audiobookInfo"]["bookFileId"] == 10157


def test_search_audiobooks_force_refreshes_legacy_cached_detail_missing_audio_shape(booklore_client):
    legacy_cached = {
        "id": 6798,
        "title": "The Mars Anomaly",
        "authors": "Joshua T. Calvert",
        "fileName": "The Mars Anomaly - Joshua T. Calvert (2024).epub",
        "bookType": "EPUB",
        "primaryFile": {
            "bookType": "EPUB",
            "fileName": "The Mars Anomaly - Joshua T. Calvert (2024).epub",
        },
        "_detail_fetched_at": time.time() - 3600,
    }
    refreshed = {
        **legacy_cached,
        "alternativeFormats": [
            {
                "id": 10157,
                "bookType": "AUDIOBOOK",
                "fileName": "The Mars Anomaly - Joshua T. Calvert (2024).m4b",
            }
        ],
        "supplementaryFiles": [],
        "audiobookMetadata": {"durationSeconds": 33945},
    }

    booklore_client._book_cache = {legacy_cached["fileName"].lower(): legacy_cached}
    booklore_client._book_id_cache = {6798: legacy_cached}
    booklore_client._cache_timestamp = time.time()
    booklore_client._fetch_and_cache_detail = MagicMock(return_value=refreshed)
    booklore_client.get_audiobook_info = MagicMock(return_value={"bookFileId": 10157})

    results = booklore_client.search_audiobooks("Mars Anomaly")

    booklore_client._fetch_and_cache_detail.assert_called_once_with(6798, force_refresh=True)
    assert len(results) == 1
    assert results[0]["id"] == 6798


def test_search_books_dedupes_stale_filename_aliases_by_book_id(booklore_client):
    stale = {
        "id": 6798,
        "title": "The Mars Anomaly",
        "authors": "Joshua T. Calvert",
        "fileName": "Mars Anomaly_ Hard Science Fiction, The - Joshua T. Calvert.epub",
        "bookType": "EPUB",
    }
    current = {
        "id": 6798,
        "title": "The Mars Anomaly",
        "authors": "Joshua T. Calvert",
        "fileName": "The Mars Anomaly - Joshua T. Calvert (2024).epub",
        "bookType": "EPUB",
    }

    booklore_client._book_cache = {
        stale["fileName"].lower(): stale,
        current["fileName"].lower(): current,
    }
    booklore_client._book_id_cache = {6798: current}
    booklore_client._cache_timestamp = time.time()

    results = booklore_client.search_books("Mars Anomaly")

    assert len(results) == 1
    assert results[0]["fileName"] == current["fileName"]


def test_process_book_detail_removes_stale_filename_aliases_for_same_id(booklore_client):
    old_name = "mars anomaly_ hard science fiction, the - joshua t. calvert.epub"
    new_name = "the mars anomaly - joshua t. calvert (2024).epub"
    booklore_client._book_cache = {
        old_name: {
            "id": 6798,
            "fileName": "Mars Anomaly_ Hard Science Fiction, The - Joshua T. Calvert.epub",
            "title": "The Mars Anomaly",
            "authors": "Joshua T. Calvert",
            "bookType": "EPUB",
        }
    }
    booklore_client._book_id_cache = {
        6798: booklore_client._book_cache[old_name]
    }

    detail = {
        "id": 6798,
        "libraryId": "lib-1",
        "metadata": {
            "title": "The Mars Anomaly",
            "authors": ["Joshua T. Calvert"],
        },
        "primaryFile": {
            "fileName": "The Mars Anomaly - Joshua T. Calvert (2024).epub",
            "filePath": "/books/The Mars Anomaly - Joshua T. Calvert (2024).epub",
            "bookType": "EPUB",
        },
    }

    booklore_client._process_book_detail(detail)

    assert old_name not in booklore_client._book_cache
    assert new_name in booklore_client._book_cache
    booklore_client.db.delete_booklore_book.assert_called_with(old_name)
