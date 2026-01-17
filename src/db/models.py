"""
SQLAlchemy ORM models for abs-kosync-bridge database.
"""

from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from typing import Optional

Base = declarative_base()


class Book(Base):
    """
    Book model storing book metadata and mapping information.
    """
    __tablename__ = 'books'

    abs_id = Column(String(255), primary_key=True)
    abs_title = Column(String(500))
    ebook_filename = Column(String(500))
    kosync_doc_id = Column(String(255))
    transcript_file = Column(String(500))
    status = Column(String(50), default='active')
    duration = Column(Float)  # Duration in seconds from AudioBookShelf

    # Relationships
    states = relationship("State", back_populates="book", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="book", cascade="all, delete-orphan")
    hardcover_details = relationship("HardcoverDetails", back_populates="book", cascade="all, delete-orphan", uselist=False)

    def __init__(self, abs_id: str, abs_title: str = None, ebook_filename: str = None,
                 kosync_doc_id: str = None, transcript_file: str = None,
                 status: str = 'active', duration: float = None):
        self.abs_id = abs_id
        self.abs_title = abs_title
        self.ebook_filename = ebook_filename
        self.kosync_doc_id = kosync_doc_id
        self.transcript_file = transcript_file
        self.status = status
        self.duration = duration

    def __repr__(self):
        return f"<Book(abs_id='{self.abs_id}', title='{self.abs_title}')>"


class HardcoverDetails(Base):
    """
    HardcoverDetails model storing hardcover book matching information.
    """
    __tablename__ = 'hardcover_details'

    abs_id = Column(String(255), ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True)
    hardcover_book_id = Column(String(255))
    hardcover_slug = Column(String(255))
    hardcover_edition_id = Column(String(255))
    hardcover_pages = Column(Integer)
    isbn = Column(String(255))
    asin = Column(String(255))
    matched_by = Column(String(50))  # 'isbn', 'asin', 'title_author', 'title'

    # Relationship
    book = relationship("Book", back_populates="hardcover_details")

    def __init__(self, abs_id: str, hardcover_book_id: str = None, hardcover_slug: str = None,
                 hardcover_edition_id: str = None,
                 hardcover_pages: int = None, isbn: str = None, asin: str = None, matched_by: str = None):
        self.abs_id = abs_id
        self.hardcover_book_id = hardcover_book_id
        self.hardcover_slug = hardcover_slug
        self.hardcover_edition_id = hardcover_edition_id
        self.hardcover_pages = hardcover_pages
        self.isbn = isbn
        self.asin = asin
        self.matched_by = matched_by

    def __repr__(self):
        return f"<HardcoverDetails(abs_id='{self.abs_id}', hardcover_book_id='{self.hardcover_book_id}')>"


class State(Base):
    """
    State model storing sync state per book and client.
    """
    __tablename__ = 'states'

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), ForeignKey('books.abs_id'), nullable=False)
    client_name = Column(String(50), nullable=False)
    last_updated = Column(Float)
    percentage = Column(Float)
    timestamp = Column(Float)
    xpath = Column(Text)
    cfi = Column(Text)

    # Relationship
    book = relationship("Book", back_populates="states")

    def __init__(self, abs_id: str, client_name: str, last_updated: float = None,
                 percentage: float = None, timestamp: float = None,
                 xpath: str = None, cfi: str = None):
        self.abs_id = abs_id
        self.client_name = client_name
        self.last_updated = last_updated
        self.percentage = percentage
        self.timestamp = timestamp
        self.xpath = xpath
        self.cfi = cfi

    def __repr__(self):
        return f"<State(abs_id='{self.abs_id}', client='{self.client_name}', pct={self.percentage})>"


class Job(Base):
    """
    Job model storing job execution data for books.
    """
    __tablename__ = 'jobs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    abs_id = Column(String(255), ForeignKey('books.abs_id'), nullable=False)
    last_attempt = Column(Float)
    retry_count = Column(Integer, default=0)
    last_error = Column(Text)

    # Relationship
    book = relationship("Book", back_populates="jobs")

    def __init__(self, abs_id: str, last_attempt: float = None,
                 retry_count: int = 0, last_error: str = None):
        self.abs_id = abs_id
        self.last_attempt = last_attempt
        self.retry_count = retry_count
        self.last_error = last_error

    def __repr__(self):
        return f"<Job(abs_id='{self.abs_id}', retries={self.retry_count})>"



class Setting(Base):
    """
    Setting model storing application configuration.
    """
    __tablename__ = 'settings'

    key = Column(String(255), primary_key=True)
    value = Column(Text, nullable=True)

    def __init__(self, key: str, value: str = None):
        self.key = key
        self.value = value

    def __repr__(self):
        return f"<Setting(key='{self.key}', value='{self.value}')>"


# Database configuration
class DatabaseManager:
    """
    Database manager handling SQLAlchemy engine and session management.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.engine = create_engine(f'sqlite:///{db_path}', echo=False)
        self.SessionLocal = sessionmaker(bind=self.engine)

        # Note: Schema creation is handled by Alembic migrations
        # No longer calling Base.metadata.create_all() here

    def get_session(self):
        """Get a new database session."""
        return self.SessionLocal()

    def close(self):
        """Close the database engine."""
        self.engine.dispose()
