"""add hardcover_audio_seconds column to hardcover_details

Revision ID: add_hardcover_audio_seconds
Revises: add_kosync_hash_cache_fields
Create Date: 2026-02-09 19:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_hardcover_audio_seconds'
down_revision: Union[str, Sequence[str], None] = 'add_kosync_hash_cache_fields'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('hardcover_details')]

    if 'hardcover_audio_seconds' not in columns:
        op.add_column('hardcover_details', sa.Column('hardcover_audio_seconds', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hardcover_details', 'hardcover_audio_seconds')
