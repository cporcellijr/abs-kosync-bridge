# [START FILE: abs-kosync-enhanced/hardcover_client.py]
"""
Hardcover.app GraphQL API Client

Handles book tracking, progress updates, and reading dates for Hardcover.app integration.

Key features:
- Auto-sets started_at when creating a new read
- Auto-sets finished_at when marking as finished (>99% progress)
- Supports ISBN and title/author search for book matching


"""

import os
import requests
import logging
from typing import Optional, Dict, Any
from datetime import date

logger = logging.getLogger(__name__)


class HardcoverClient:
    def __init__(self):
        self.api_url = "https://api.hardcover.app/v1/graphql"
        self.token = os.environ.get("HARDCOVER_TOKEN")
        self.user_id = None
        
        if not self.token:
            logger.warning("HARDCOVER_TOKEN not set")
            return
        
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "ABS-KoSync-Enhanced/5.9"
        }
    
    def query(self, query: str, variables: Dict = None) -> Optional[Dict]:
        if not self.token:
            return None
        
        try:
            r = requests.post(
                self.api_url,
                json={"query": query, "variables": variables or {}},
                headers=self.headers,
                timeout=10
            )
            
            if r.status_code == 200:
                data = r.json()
                if data.get('data'):
                    return data['data']
                elif data.get('errors'):
                    logger.error(f"GraphQL errors: {data['errors']}")
            else:
                logger.error(f"HTTP {r.status_code}: {r.text}")
        except Exception as e:
            logger.error(f"Hardcover query failed: {e}")
        
        return None
    
    def get_user_id(self) -> Optional[int]:
        if self.user_id:
            return self.user_id
        
        result = self.query("{ me { id } }")
        if result and result.get('me'):
            self.user_id = result['me'][0]['id']
        return self.user_id
    
    def search_by_isbn(self, isbn: str) -> Optional[Dict]:
        """Search by ISBN-13 or ISBN-10."""
        isbn_key = 'isbn_13' if len(str(isbn)) == 13 else 'isbn_10'
        
        query = f"""
        query ($isbn: String!) {{
            editions(where: {{ {isbn_key}: {{ _eq: $isbn }} }}) {{
                id
                pages
                book {{
                    id
                    title
                }}
            }}
        }}
        """
        
        result = self.query(query, {"isbn": str(isbn)})
        if result and result.get('editions') and len(result['editions']) > 0:
            edition = result['editions'][0]
            return {
                'book_id': edition['book']['id'],
                'edition_id': edition['id'],
                'pages': edition['pages'],
                'title': edition['book']['title']
            }
        return None
    
    def search_by_title_author(self, title: str, author: str = None) -> Optional[Dict]:
        """Search by title and author."""
        search_query = f"{title} {author or ''}".strip()
        
        query = """
        query ($query: String!) {
            search(query: $query, per_page: 5, page: 1, query_type: "Book") {
                ids
            }
        }
        """
        
        result = self.query(query, {"query": search_query})
        if not result or not result.get('search') or not result['search'].get('ids'):
            return None
        
        book_ids = result['search']['ids']
        if not book_ids:
            return None
        
        book_query = """
        query ($id: Int!) {
            books(where: { id: { _eq: $id }}) {
                id
                title
            }
        }
        """
        
        book_result = self.query(book_query, {"id": book_ids[0]})
        if book_result and book_result.get('books') and len(book_result['books']) > 0:
            book = book_result['books'][0]
            edition = self.get_default_edition(book['id'])
            
            return {
                'book_id': book['id'],
                'edition_id': edition.get('id') if edition else None,
                'pages': edition.get('pages') if edition else None,
                'title': book['title']
            }
        
        return None
    
    def get_default_edition(self, book_id: int) -> Optional[Dict]:
        """Get default edition for a book."""
        query = """
        query ($bookId: Int!) {
            books_by_pk(id: $bookId) {
                default_ebook_edition {
                    id
                    pages
                }
                default_physical_edition {
                    id
                    pages
                }
            }
        }
        """
        
        result = self.query(query, {"bookId": book_id})
        if result and result.get('books_by_pk'):
            if result['books_by_pk'].get('default_ebook_edition'):
                return result['books_by_pk']['default_ebook_edition']
            elif result['books_by_pk'].get('default_physical_edition'):
                return result['books_by_pk']['default_physical_edition']
        
        return None
    
    def find_user_book(self, book_id: int) -> Optional[Dict]:
        """Find existing user_book with read info."""
        query = """
        query ($bookId: Int!, $userId: Int!) {
            user_books(where: { book_id: { _eq: $bookId }, user_id: { _eq: $userId }}) {
                id
                status_id
                edition_id
                user_book_reads(order_by: {id: desc}, limit: 1) {
                    id
                    started_at
                    finished_at
                    progress_pages
                }
            }
        }
        """
        
        result = self.query(query, {"bookId": book_id, "userId": self.get_user_id()})
        if result and result.get('user_books') and len(result['user_books']) > 0:
            return result['user_books'][0]
        return None
    
    def update_status(self, book_id: int, status_id: int, edition_id: int = None) -> Optional[Dict]:
        """
        Create/update user_book status.
        
        Status IDs:
        - 1: Want to Read
        - 2: Currently Reading  
        - 3: Read (Finished)
        - 4: Did Not Finish
        """
        query = """
        mutation ($object: UserBookCreateInput!) {
            insert_user_book(object: $object) {
                error
                user_book {
                    id
                    status_id
                    edition_id
                }
            }
        }
        """
        
        update_args = {
            "book_id": book_id,
            "status_id": status_id,
            "privacy_setting_id": 1
        }
        
        if edition_id:
            update_args["edition_id"] = edition_id
        
        result = self.query(query, {"object": update_args})
        if result and result.get('insert_user_book'):
            error = result['insert_user_book'].get('error')
            if error:
                logger.error(f"Hardcover update_status error: {error}")
            return result['insert_user_book'].get('user_book')
        return None
    
    def _get_today_date(self) -> str:
        """Get today's date in YYYY-MM-DD format for Hardcover API."""
        return date.today().isoformat()
    
    def update_progress(self, user_book_id: int, page: int, edition_id: int = None, is_finished: bool = False) -> bool:
        """
        Update reading progress with proper date handling.
        
        CRITICAL: Matches hardcover_api.lua lines 539-571 exactly!
        started_at and finished_at are TOP-LEVEL variables, NOT in object!
        
        Features:
        - Sets started_at to today when creating a new read (if not set)
        - Sets finished_at to today when is_finished=True
        - Updates progress_pages
        
        Args:
            user_book_id: The Hardcover user_book ID
            page: Current page number
            edition_id: Optional edition ID for the specific format
            is_finished: If True, sets finished_at date
        
        Returns:
            True if successful, False otherwise
        """
        # First check if there's an existing read
        read_query = """
        query ($userBookId: Int!) {
            user_book_reads(where: { user_book_id: { _eq: $userBookId }}, order_by: {id: desc}, limit: 1) {
                id
                started_at
                finished_at
            }
        }
        """
        
        read_result = self.query(read_query, {"userBookId": user_book_id})
        today = self._get_today_date()
        
        if read_result and read_result.get('user_book_reads') and len(read_result['user_book_reads']) > 0:
            # Update existing read - MATCHES hardcover_api.lua line 539-571
            existing_read = read_result['user_book_reads'][0]
            read_id = existing_read['id']
            
            # FIX: Initialize with EXISTING values to prevent wiping dates with Null
            started_at_val = existing_read.get('started_at')
            finished_at_val = existing_read.get('finished_at')
            
            if not started_at_val:
                started_at_val = today
                logger.info(f"Hardcover: Setting started_at to {today}")
            
            if is_finished and not finished_at_val:
                finished_at_val = today
                logger.info(f"Hardcover: Setting finished_at to {today}")
            
            # 
            query = """
            mutation UpdateBookProgress($id: Int!, $pages: Int, $editionId: Int, $startedAt: date, $finishedAt: date) {
                update_user_book_read(id: $id, object: {
                    progress_pages: $pages,
                    edition_id: $editionId,
                    started_at: $startedAt,
                    finished_at: $finishedAt
                }) {
                    error
                    user_book_read {
                        id
                        started_at
                        finished_at
                        edition_id
                        progress_pages
                    }
                }
            }
            """
            
            result = self.query(query, {
                "id": read_id, 
                "pages": page, 
                "editionId": edition_id,
                "startedAt": started_at_val,
                "finishedAt": finished_at_val
            })
            
            if result and result.get('update_user_book_read'):
                error = result['update_user_book_read'].get('error')
                if error:
                    logger.error(f"Hardcover update_user_book_read error: {error}")
                    return False
                logger.debug(f"Hardcover: Updated read {read_id} -> page {page}")
                return True
            return False
        else:
         
            query = """
            mutation InsertUserBookRead($id: Int!, $pages: Int, $editionId: Int, $startedAt: date, $finishedAt: date) {
                insert_user_book_read(user_book_id: $id, user_book_read: {
                    progress_pages: $pages,
                    edition_id: $editionId,
                    started_at: $startedAt,
                    finished_at: $finishedAt
                }) {
                    error
                    user_book_read {
                        id
                        started_at
                        finished_at
                        edition_id
                        progress_pages
                    }
                }
            }
            """
            
            finished_at_val = today if is_finished else None
            
            result = self.query(query, {
                "id": user_book_id, 
                "pages": page, 
                "editionId": edition_id,
                "startedAt": today,
                "finishedAt": finished_at_val
            })
            
            if result and result.get('insert_user_book_read'):
                error = result['insert_user_book_read'].get('error')
                if error:
                    logger.error(f"Hardcover insert_user_book_read error: {error}")
                    return False
                logger.info(f"Hardcover: Created new read for user_book {user_book_id} (started: {today})")
                return True
            return False
# [END FILE]