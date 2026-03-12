"""add generic audio source columns to books

Revision ID: a7c9e1d3f4b2
Revises: f6b2c4d8e9a1
Create Date: 2026-03-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c9e1d3f4b2"
down_revision: Union[str, Sequence[str], None] = "f6b2c4d8e9a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    if column.name not in columns:
        op.add_column(table_name, column)


def _drop_column_if_present(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns(table_name)}
    if column_name in columns:
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing("books", sa.Column("audio_source", sa.String(length=32), nullable=True))
    _add_column_if_missing("books", sa.Column("audio_source_id", sa.String(length=255), nullable=True))
    _add_column_if_missing("books", sa.Column("audio_title", sa.String(length=500), nullable=True))
    _add_column_if_missing("books", sa.Column("audio_cover_url", sa.String(length=1000), nullable=True))
    _add_column_if_missing("books", sa.Column("audio_duration", sa.Float(), nullable=True))
    _add_column_if_missing("books", sa.Column("audio_provider_book_id", sa.String(length=255), nullable=True))
    _add_column_if_missing("books", sa.Column("audio_provider_file_id", sa.String(length=255), nullable=True))
    _add_column_if_missing("books", sa.Column("ebook_source", sa.String(length=32), nullable=True))
    _add_column_if_missing("books", sa.Column("ebook_source_id", sa.String(length=255), nullable=True))

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("books")}
    if "ix_books_audio_source" not in indexes:
        op.create_index("ix_books_audio_source", "books", ["audio_source"])
    if "ix_books_audio_source_id" not in indexes:
        op.create_index("ix_books_audio_source_id", "books", ["audio_source_id"])

    books = sa.table(
        "books",
        sa.column("abs_id", sa.String()),
        sa.column("abs_title", sa.String()),
        sa.column("audio_source", sa.String()),
        sa.column("audio_source_id", sa.String()),
        sa.column("audio_title", sa.String()),
        sa.column("audio_duration", sa.Float()),
        sa.column("duration", sa.Float()),
        sa.column("sync_mode", sa.String()),
    )
    bind.execute(
        books.update()
        .where(books.c.sync_mode != "ebook_only")
        .values(
            audio_source="ABS",
            audio_source_id=books.c.abs_id,
            audio_title=books.c.abs_title,
            audio_duration=books.c.duration,
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {idx["name"] for idx in inspector.get_indexes("books")}
    if "ix_books_audio_source_id" in indexes:
        op.drop_index("ix_books_audio_source_id", table_name="books")
    if "ix_books_audio_source" in indexes:
        op.drop_index("ix_books_audio_source", table_name="books")

    _drop_column_if_present("books", "ebook_source_id")
    _drop_column_if_present("books", "ebook_source")
    _drop_column_if_present("books", "audio_provider_file_id")
    _drop_column_if_present("books", "audio_provider_book_id")
    _drop_column_if_present("books", "audio_duration")
    _drop_column_if_present("books", "audio_cover_url")
    _drop_column_if_present("books", "audio_title")
    _drop_column_if_present("books", "audio_source_id")
    _drop_column_if_present("books", "audio_source")
