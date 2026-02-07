"""add_storyteller_uuid

Revision ID: bc2f5eb57a69
Revises: 
Create Date: 2026-02-07 08:58:34.123456

"""
from alembic import op
import sqlalchemy as sa



# revision identifiers, used by Alembic.
revision = 'bc2f5eb57a69'
down_revision = 'add_kosync_hash_cache_fields'

def upgrade() -> None:
    op.add_column('books', sa.Column('storyteller_uuid', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_books_storyteller_uuid'), 'books', ['storyteller_uuid'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_books_storyteller_uuid'), table_name='books')
    op.drop_column('books', 'storyteller_uuid')
