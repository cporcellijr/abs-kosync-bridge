"""ensure all expected columns and tables exist

This is a comprehensive safety migration that idempotently verifies
the schema matches the current model definitions. It catches any columns
or tables that may have been missed due to migration chain issues.

Revision ID: fix_original_filename
Revises: add_hardcover_audio_seconds
Create Date: 2026-02-18

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'fix_original_filename'
down_revision = 'add_hardcover_audio_seconds'
branch_labels = None
depends_on = None


def _get_columns(inspector, table_name: str) -> set:
    """Get the set of column names for a table, or empty set if table missing."""
    if table_name not in inspector.get_table_names():
        return set()
    return {c['name'] for c in inspector.get_columns(table_name)}


def _get_indexes(inspector, table_name: str) -> set:
    """Get the set of index names for a table."""
    if table_name not in inspector.get_table_names():
        return set()
    return {idx['name'] for idx in inspector.get_indexes(table_name)}


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()

    # ── books table ──────────────────────────────────────────────────
    books_cols = _get_columns(inspector, 'books')
    if 'sync_mode' not in books_cols:
        op.add_column('books', sa.Column('sync_mode', sa.String(20), nullable=True, server_default='audiobook'))
    if 'original_ebook_filename' not in books_cols:
        op.add_column('books', sa.Column('original_ebook_filename', sa.String(500), nullable=True))
    if 'storyteller_uuid' not in books_cols:
        op.add_column('books', sa.Column('storyteller_uuid', sa.String(36), nullable=True))
    if 'abs_ebook_item_id' not in books_cols:
        op.add_column('books', sa.Column('abs_ebook_item_id', sa.String(255), nullable=True))

    books_indexes = _get_indexes(inspector, 'books')
    if 'storyteller_uuid' in _get_columns(inspector, 'books') and 'ix_books_storyteller_uuid' not in books_indexes:
        op.create_index(op.f('ix_books_storyteller_uuid'), 'books', ['storyteller_uuid'], unique=False)

    # ── hardcover_details table ──────────────────────────────────────
    hc_cols = _get_columns(inspector, 'hardcover_details')
    if 'hardcover_slug' not in hc_cols:
        op.add_column('hardcover_details', sa.Column('hardcover_slug', sa.String(255), nullable=True))
    if 'hardcover_audio_seconds' not in hc_cols:
        op.add_column('hardcover_details', sa.Column('hardcover_audio_seconds', sa.Integer(), nullable=True))

    # ── kosync_documents table ───────────────────────────────────────
    if 'kosync_documents' in tables:
        kd_cols = _get_columns(inspector, 'kosync_documents')
        if 'filename' not in kd_cols:
            op.add_column('kosync_documents', sa.Column('filename', sa.String(500), nullable=True))
        if 'source' not in kd_cols:
            op.add_column('kosync_documents', sa.Column('source', sa.String(50), nullable=True))
        if 'booklore_id' not in kd_cols:
            op.add_column('kosync_documents', sa.Column('booklore_id', sa.String(255), nullable=True))
        if 'mtime' not in kd_cols:
            op.add_column('kosync_documents', sa.Column('mtime', sa.Float(), nullable=True))

        kd_indexes = _get_indexes(inspector, 'kosync_documents')
        if 'booklore_id' in _get_columns(inspector, 'kosync_documents') and 'ix_kosync_documents_booklore_id' not in kd_indexes:
            op.create_index(op.f('ix_kosync_documents_booklore_id'), 'kosync_documents', ['booklore_id'], unique=False)

    # ── jobs table ───────────────────────────────────────────────────
    jobs_cols = _get_columns(inspector, 'jobs')
    if 'progress' not in jobs_cols:
        op.add_column('jobs', sa.Column('progress', sa.Float(), nullable=True, server_default='0.0'))

    # ── settings table ───────────────────────────────────────────────
    if 'settings' not in tables:
        op.create_table('settings',
            sa.Column('key', sa.String(length=255), nullable=False),
            sa.Column('value', sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint('key')
        )

    # ── pending_suggestions table ────────────────────────────────────
    if 'pending_suggestions' not in tables:
        op.create_table('pending_suggestions',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('source', sa.String(50), nullable=True, server_default='abs'),
            sa.Column('source_id', sa.String(255), nullable=True),
            sa.Column('title', sa.String(500), nullable=True),
            sa.Column('author', sa.String(500), nullable=True),
            sa.Column('cover_url', sa.String(500), nullable=True),
            sa.Column('matches_json', sa.Text(), nullable=True),
            sa.Column('status', sa.String(20), nullable=True, server_default='pending'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )

    # ── book_alignments table ────────────────────────────────────────
    if 'book_alignments' not in tables:
        op.create_table('book_alignments',
            sa.Column('abs_id', sa.String(255), sa.ForeignKey('books.abs_id', ondelete='CASCADE'), primary_key=True),
            sa.Column('alignment_map_json', sa.Text(), nullable=True),
            sa.Column('last_updated', sa.DateTime(), nullable=True),
        )
    else:
        ba_cols = _get_columns(inspector, 'book_alignments')
        if 'last_updated' not in ba_cols:
            op.add_column('book_alignments', sa.Column('last_updated', sa.DateTime(), nullable=True))

    # ── booklore_books table ─────────────────────────────────────────
    if 'booklore_books' not in tables:
        op.create_table('booklore_books',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('filename', sa.String(500), nullable=True),
            sa.Column('title', sa.String(500), nullable=True),
            sa.Column('authors', sa.String(500), nullable=True),
            sa.Column('raw_metadata', sa.Text(), nullable=True),
        )


def downgrade():
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.drop_column('original_ebook_filename')
        batch_op.drop_column('storyteller_uuid')
        batch_op.drop_column('abs_ebook_item_id')
        batch_op.drop_column('sync_mode')
    with op.batch_alter_table('hardcover_details', schema=None) as batch_op:
        batch_op.drop_column('hardcover_slug')
        batch_op.drop_column('hardcover_audio_seconds')
