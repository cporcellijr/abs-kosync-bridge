"""add original_ebook_filename

Revision ID: add_original_filename
Revises: add_storyteller_uuid
Create Date: 2026-02-07 10:45:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'add_original_filename'
down_revision = 'bc2f5eb57a69' # Ensure this matches the previous revision ID
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.add_column(sa.Column('original_ebook_filename', sa.String(length=500), nullable=True))

def downgrade():
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.drop_column('original_ebook_filename')