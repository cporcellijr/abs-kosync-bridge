"""add hash cache columns to kosync_documents
 
Revision ID: add_kosync_hash_cache_fields
Revises: add_sync_mode_column
Create Date: 2026-02-06 02:25:00.000000
 
"""
from typing import Sequence, Union
 
from alembic import op
import sqlalchemy as sa
 
 
# revision identifiers, used by Alembic.
revision: str = 'add_kosync_hash_cache_fields'
down_revision: Union[str, Sequence[str], None] = 'add_sync_mode_column'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
 
 
def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('kosync_documents')]
 
    if 'filename' not in columns:
        op.add_column('kosync_documents', sa.Column('filename', sa.String(length=500), nullable=True))
    if 'source' not in columns:
        op.add_column('kosync_documents', sa.Column('source', sa.String(length=50), nullable=True))
    if 'booklore_id' not in columns:
        op.add_column('kosync_documents', sa.Column('booklore_id', sa.String(length=255), nullable=True))
        op.create_index(op.f('ix_kosync_documents_booklore_id'), 'kosync_documents', ['booklore_id'], unique=False)
    if 'mtime' not in columns:
        op.add_column('kosync_documents', sa.Column('mtime', sa.Float(), nullable=True))
 
 
def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_kosync_documents_booklore_id'), table_name='kosync_documents')
    op.drop_column('kosync_documents', 'mtime')
    op.drop_column('kosync_documents', 'booklore_id')
    op.drop_column('kosync_documents', 'source')
    op.drop_column('kosync_documents', 'filename')
