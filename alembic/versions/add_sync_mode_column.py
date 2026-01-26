"""add sync_mode to books

Revision ID: add_sync_mode_column
Revises: 4dced57540b5
Create Date: 2026-01-21 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_sync_mode_column'
down_revision: Union[str, Sequence[str], None] = '43be53e3830a'  # Latest migration: add_kosync_documents_table
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - add sync_mode column to books table."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('books')]

    if 'sync_mode' not in columns:
        op.add_column('books', sa.Column('sync_mode', sa.String(length=20), server_default='audiobook', nullable=False))


def downgrade() -> None:
    """Downgrade schema - remove sync_mode column from books table."""
    op.drop_column('books', 'sync_mode')
