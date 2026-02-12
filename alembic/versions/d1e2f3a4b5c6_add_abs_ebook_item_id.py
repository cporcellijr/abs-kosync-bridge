"""add abs_ebook_item_id column

Revision ID: d1e2f3a4b5c6
Revises: bc2f5eb57a69
Create Date: 2026-02-12 12:25:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd1e2f3a4b5c6'
down_revision = 'add_original_filename'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add abs_ebook_item_id column to books table
    op.add_column('books', sa.Column('abs_ebook_item_id', sa.String(length=255), nullable=True))


def downgrade() -> None:
    # Remove abs_ebook_item_id column from books table
    op.drop_column('books', 'abs_ebook_item_id')
