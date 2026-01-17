"""add hardcover_slug column

Revision ID: 4dced57540b5
Revises: add_settings_table
Create Date: 2026-01-17 15:47:37.870156

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_hardcover_slug_column'
down_revision: Union[str, Sequence[str], None] = 'add_settings_table'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('hardcover_details', sa.Column('hardcover_slug', sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('hardcover_details', 'hardcover_slug')
