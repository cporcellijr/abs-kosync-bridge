"""ensure original_ebook_filename exists

Revision ID: fix_original_filename
Revises: d1e2f3a4b5c6
Create Date: 2026-02-18

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'fix_original_filename'
down_revision = 'add_hardcover_audio_seconds'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = [c['name'] for c in inspector.get_columns('books')]
    if 'original_ebook_filename' not in columns:
        op.add_column('books', sa.Column('original_ebook_filename', sa.String(500), nullable=True))


def downgrade():
    with op.batch_alter_table('books', schema=None) as batch_op:
        batch_op.drop_column('original_ebook_filename')
