"""
Tests for KOSync server functionality.
Verifies compatibility with kosync-dotnet behavior.
"""
import unittest
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
import os
import shutil

# Set test environment
TEST_DIR = '/tmp/test_kosync'
os.environ['DATA_DIR'] = TEST_DIR
os.environ['KOSYNC_USER'] = 'testuser'
os.environ['KOSYNC_KEY'] = 'testpass'


# Ensure test directory exists
if os.path.exists(TEST_DIR):
    shutil.rmtree(TEST_DIR)
os.makedirs(TEST_DIR, exist_ok=True)

from src.db.models import KosyncDocument, Book, State
# Initialize DB service with test path
from src.db.database_service import DatabaseService


class TestKosyncDocument(unittest.TestCase):
    """Test KosyncDocument model and database operations."""

    @classmethod
    def setUpClass(cls):
        """Set up test database."""
        cls.db_path = os.path.join(TEST_DIR, 'test.db')
        cls.db_service = DatabaseService(cls.db_path)

    def setUp(self):
        """Clean tables before each test."""
        with self.db_service.get_session() as session:
            session.query(KosyncDocument).delete()
            session.query(State).delete()
            session.query(Book).delete()

    def test_create_kosync_document(self):
        """Test creating a new KOSync document."""
        doc = KosyncDocument(
            document_hash='a' * 32,
            progress='/body/div[1]/p[1]',
            percentage=0.25,
            device='TestDevice',
            device_id='TEST123'
        )
        saved = self.db_service.save_kosync_document(doc)

        self.assertEqual(saved.document_hash, 'a' * 32)
        # Handle float/decimal comparison loosely
        self.assertAlmostEqual(float(saved.percentage), 0.25)
        self.assertEqual(saved.device, 'TestDevice')

    def test_get_kosync_document(self):
        """Test retrieving a KOSync document."""
        # Create first
        doc = KosyncDocument(
            document_hash='b' * 32,
            percentage=0.5
        )
        self.db_service.save_kosync_document(doc)

        # Retrieve
        retrieved = self.db_service.get_kosync_document('b' * 32)
        self.assertIsNotNone(retrieved)
        self.assertAlmostEqual(float(retrieved.percentage), 0.5)

    def test_get_nonexistent_document(self):
        """Test retrieving a document that doesn't exist."""
        retrieved = self.db_service.get_kosync_document('nonexistent' + '0' * 21)
        self.assertIsNone(retrieved)

    def test_update_kosync_document(self):
        """Test updating an existing KOSync document."""
        doc = KosyncDocument(
            document_hash='c' * 32,
            percentage=0.1
        )
        self.db_service.save_kosync_document(doc)

        # Update
        doc.percentage = 0.9
        doc.progress = '/body/div[99]'
        self.db_service.save_kosync_document(doc)

        # Verify
        retrieved = self.db_service.get_kosync_document('c' * 32)
        self.assertAlmostEqual(float(retrieved.percentage), 0.9)
        self.assertEqual(retrieved.progress, '/body/div[99]')

    def test_link_kosync_document(self):
        """Test linking a document to an ABS book."""
        # Create doc
        doc = KosyncDocument(
            document_hash='d' * 32,
            percentage=0.3
        )
        self.db_service.save_kosync_document(doc)

        # Create book
        book = Book(abs_id="book-1", abs_title="Test Book")
        self.db_service.save_book(book)

        # Link
        result = self.db_service.link_kosync_document('d' * 32, 'book-1')
        self.assertTrue(result)

        # Verify
        retrieved = self.db_service.get_kosync_document('d' * 32)
        self.assertEqual(retrieved.linked_abs_id, 'book-1')

    def test_get_unlinked_documents(self):
        """Test retrieving unlinked documents."""
        doc = KosyncDocument(
            document_hash='e' * 32,
            percentage=0.4
        )
        self.db_service.save_kosync_document(doc)

        unlinked = self.db_service.get_unlinked_kosync_documents()
        hashes = [d.document_hash for d in unlinked]
        self.assertIn('e' * 32, hashes)

    def test_delete_kosync_document(self):
        """Test deleting a KOSync document."""
        doc = KosyncDocument(
            document_hash='f' * 32,
            percentage=0.6
        )
        self.db_service.save_kosync_document(doc)

        # Delete
        result = self.db_service.delete_kosync_document('f' * 32)
        self.assertTrue(result)

        # Verify gone
        retrieved = self.db_service.get_kosync_document('f' * 32)
        self.assertIsNone(retrieved)


class TestKosyncEndpoints(unittest.TestCase):
    """Test KOSync HTTP endpoints."""

    @classmethod
    def setUpClass(cls):
        # Setup DB one time
        cls.db_path = os.path.join(TEST_DIR, 'test.db')
        # Ensure DB service is initialized in web_server logic
        # We need to monkeypatch the global database_service in web_server
        from src import web_server
        web_server.database_service = DatabaseService(cls.db_path)
        if not hasattr(web_server, 'app'):
            web_server.app, _ = web_server.create_app()
        cls.app = web_server.app
        cls.client = cls.app.test_client()

    def setUp(self):
        # Auth headers
        import hashlib
        self.auth_headers = {
            'x-auth-user': 'testuser',
            'x-auth-key': hashlib.md5(b'testpass').hexdigest(),
            'Content-Type': 'application/json'
        }
        # Clear specific tables
        from src import web_server
        with web_server.database_service.get_session() as session:
             session.query(KosyncDocument).delete()

    def test_put_progress_creates_document(self):
        """Test that PUT creates a new document."""
        # Case 1: Standard device (should return String timestamp)
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'g' * 32,
                'progress': '/body/test',
                'percentage': 0.33,
                'device': 'TestKobo',
                'device_id': 'KOBO123'
            }
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['document'], 'g' * 32)
        self.assertIn('timestamp', data)
        # PUT response timestamp should be ISO 8601 string (kosync-dotnet behavior)
        self.assertIsInstance(data['timestamp'], str)
        self.assertIn('T', data['timestamp'])  # ISO format contains 'T'

        # Case 2: BookNexus device (should return Int timestamp)
        response_bn = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'bn' * 16,
                'progress': '/body/test2',
                'percentage': 0.44,
                'device': 'BookNexus',
                'device_id': 'BN123'
            }
        )
        self.assertEqual(response_bn.status_code, 200)
        data_bn = response_bn.get_json()
        self.assertIsInstance(data_bn['timestamp'], int)

    def test_get_progress_returns_502_for_missing(self):
        """Test that GET returns 502 (not 404) for missing document."""
        response = self.client.get(
            '/syncs/progress/' + 'z' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 502)
        data = response.get_json()
        self.assertIn('message', data)
        self.assertIn('not found', data['message'].lower())

    def test_get_progress_returns_full_data(self):
        """Test that GET returns all fields."""
        # First PUT
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'h' * 32,
                'progress': '/body/chapter[5]',
                'percentage': 0.55,
                'device': 'TestKindle',
                'device_id': 'KINDLE456'
            }
        )

        # Then GET
        response = self.client.get(
            '/syncs/progress/' + 'h' * 32,
            headers=self.auth_headers
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()

        # Verify all fields present (matching kosync-dotnet)
        self.assertEqual(data['document'], 'h' * 32)
        self.assertEqual(data['progress'], '/body/chapter[5]')
        self.assertAlmostEqual(data['percentage'], 0.55)
        self.assertEqual(data['device'], 'TestKindle')
        self.assertEqual(data['device_id'], 'KINDLE456')
        self.assertIn('timestamp', data)

    def test_furthest_wins_rejects_backwards(self):
        """Test that backwards progress is rejected when KOSYNC_FURTHEST_WINS=true."""
        # First PUT at 50%
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'i' * 32,
                'percentage': 0.50,
                'progress': '/body/middle',
                'device': 'Device1',
                'device_id': 'D1'
            }
        )

        # Try to go backwards to 25% - should be REJECTED
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'i' * 32,
                'percentage': 0.25,
                'progress': '/body/earlier',
                'device': 'Device2',
                'device_id': 'D2'
            }
        )

        self.assertEqual(response.status_code, 200)

        # Verify progress stayed at 50% (not overwritten)
        get_response = self.client.get(
            '/syncs/progress/' + 'i' * 32,
            headers=self.auth_headers
        )
        data = get_response.get_json()
        self.assertAlmostEqual(data['percentage'], 0.50)

    def test_furthest_wins_allows_equal(self):
        """Test that equal progress values are accepted (not rejected as backwards)."""
        # First PUT at 50%
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'j' * 32,
                'percentage': 0.50,
                'progress': '/body/middle',
                'device': 'Device1',
                'device_id': 'D1'
            }
        )

        # Send same percentage again - should be ACCEPTED
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'j' * 32,
                'percentage': 0.50,
                'progress': '/body/middle-updated',
                'device': 'Device2',
                'device_id': 'D2'
            }
        )

        self.assertEqual(response.status_code, 200)

        # Verify progress field was updated (same percentage, different xpath)
        get_response = self.client.get(
            '/syncs/progress/' + 'j' * 32,
            headers=self.auth_headers
        )
        data = get_response.get_json()
        self.assertEqual(data['progress'], '/body/middle-updated')
        self.assertEqual(data['device'], 'Device2')

    def test_furthest_wins_allows_forward(self):
        """Test that forward progress is accepted."""
        # First PUT at 25%
        self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'k' * 32,
                'percentage': 0.25,
                'progress': '/body/early',
                'device': 'Device1',
                'device_id': 'D1'
            }
        )

        # Go forward to 75% - should be ACCEPTED
        response = self.client.put(
            '/syncs/progress',
            headers=self.auth_headers,
            json={
                'document': 'k' * 32,
                'percentage': 0.75,
                'progress': '/body/later',
                'device': 'Device2',
                'device_id': 'D2'
            }
        )

        self.assertEqual(response.status_code, 200)

        # Verify progress moved forward
        get_response = self.client.get(
            '/syncs/progress/' + 'k' * 32,
            headers=self.auth_headers
        )
        data = get_response.get_json()
        self.assertAlmostEqual(data['percentage'], 0.75)


if __name__ == '__main__':
    unittest.main()
