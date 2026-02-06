import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.db.models import Base, Book, BookAlignment, BookloreBook

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
