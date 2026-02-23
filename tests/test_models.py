import os
import sqlite3
import tempfile
import pytest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.db.models import Base, Book, BookAlignment, BookloreBook, PendingSuggestion

@pytest.fixture
def session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()

def test_book_alignment_model(session):
    book = Book(abs_id="test_book", abs_title="Test Book")
    session.add(book)
    session.commit()
    
    alignment = BookAlignment(abs_id="test_book", alignment_map_json='[{"char":0, "ts":0}]')
    session.add(alignment)
    session.commit()
    
    retrieved = session.query(BookAlignment).filter_by(abs_id="test_book").first()
    assert retrieved is not None
    assert "char" in retrieved.alignment_map_json
    assert retrieved.book.abs_title == "Test Book"

def test_booklore_book_model(session):
    cached = BookloreBook(
        filename="test.epub", 
        title="Test Title", 
        authors="Test Author",
        raw_metadata="{}"
    )
    session.add(cached)
    session.commit()
    
    retrieved = session.query(BookloreBook).filter_by(filename="test.epub").first()
    assert retrieved.title == "Test Title"
    assert retrieved.last_updated is not None


def test_pending_suggestion_matches_corrupt_json(session):
    """Verify PendingSuggestion.matches returns [] on corrupt JSON."""
    suggestion = PendingSuggestion(
        source_id="test-hash",
        title="Test Book",
        matches_json="{not valid json!!"
    )
    session.add(suggestion)
    session.commit()

    retrieved = session.query(PendingSuggestion).first()
    assert retrieved.matches == []


def test_pending_suggestion_matches_valid_json(session):
    """Verify PendingSuggestion.matches works with valid JSON."""
    suggestion = PendingSuggestion(
        source_id="test-hash-2",
        title="Test Book 2",
        matches_json='[{"source": "abs", "abs_id": "123"}]'
    )
    session.add(suggestion)
    session.commit()

    retrieved = session.query(PendingSuggestion).first()
    assert len(retrieved.matches) == 1
    assert retrieved.matches[0]["source"] == "abs"


def test_pending_suggestion_matches_none(session):
    """Verify PendingSuggestion.matches returns [] when matches_json is None."""
    suggestion = PendingSuggestion(
        source_id="test-hash-3",
        title="Test Book 3",
        matches_json=None
    )
    session.add(suggestion)
    session.commit()

    retrieved = session.query(PendingSuggestion).first()
    assert retrieved.matches == []


def test_booklore_raw_metadata_dict_corrupt_json(session):
    """Verify BookloreBook.raw_metadata_dict returns {} on corrupt JSON."""
    book = BookloreBook(
        filename="corrupt.epub",
        title="Corrupt",
        raw_metadata="<<<not json>>>"
    )
    session.add(book)
    session.commit()

    retrieved = session.query(BookloreBook).filter_by(filename="corrupt.epub").first()
    assert retrieved.raw_metadata_dict == {}


def test_booklore_raw_metadata_dict_none(session):
    """Verify BookloreBook.raw_metadata_dict returns {} when raw_metadata is None."""
    book = BookloreBook(
        filename="none.epub",
        title="None Metadata",
        raw_metadata=None
    )
    session.add(book)
    session.commit()

    retrieved = session.query(BookloreBook).filter_by(filename="none.epub").first()
    assert retrieved.raw_metadata_dict == {}


