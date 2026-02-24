"""repair schema drift and missing tables

Revision ID: e4a1c2d9f7b3
Revises: fix_original_filename
Create Date: 2026-02-24
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e4a1c2d9f7b3'
down_revision: Union[str, Sequence[str], None] = 'fix_original_filename'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _get_columns(inspector, table_name: str) -> set:
    if table_name not in inspector.get_table_names():
        return set()
    return {c['name'] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. booklore_books
    if 'booklore_books' not in inspector.get_table_names():
        op.create_table(
            'booklore_books',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('filename', sa.String(length=500), nullable=False),
            sa.Column('title', sa.String(length=500), nullable=True),
            sa.Column('authors', sa.String(length=500), nullable=True),
            sa.Column('raw_metadata', sa.Text(), nullable=True),
            sa.Column('last_updated', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('filename', name='uq_booklore_books_filename'),
        )
        op.create_index(op.f('ix_booklore_books_filename'), 'booklore_books', ['filename'], unique=False)
    else:
        booklore_cols = _get_columns(inspector, 'booklore_books')
        if 'last_updated' not in booklore_cols:
            op.add_column('booklore_books', sa.Column('last_updated', sa.DateTime(), nullable=True))

    # 2. pending_suggestions
    if 'pending_suggestions' not in inspector.get_table_names():
        op.create_table(
            'pending_suggestions',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('source', sa.String(length=50), nullable=True, server_default='abs'),
            sa.Column('source_id', sa.String(length=255), nullable=True),
            sa.Column('title', sa.String(length=500), nullable=True),
            sa.Column('author', sa.String(length=500), nullable=True),
            sa.Column('cover_url', sa.String(length=500), nullable=True),
            sa.Column('matches_json', sa.Text(), nullable=True),
            sa.Column('status', sa.String(length=20), nullable=True, server_default='pending'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )

    # 3. book_alignments
    if 'book_alignments' not in inspector.get_table_names():
        op.create_table(
            'book_alignments',
            sa.Column('abs_id', sa.String(length=255), nullable=False),
            sa.Column('alignment_map_json', sa.Text(), nullable=False),
            sa.Column('last_updated', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['abs_id'], ['books.abs_id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('abs_id'),
        )


def downgrade() -> None:
    pass
