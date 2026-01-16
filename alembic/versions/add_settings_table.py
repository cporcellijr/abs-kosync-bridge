"""create settings table

Revision ID: add_settings_table
Revises: 
Create Date: 2024-01-20 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_settings_table'
down_revision = '76886bc89d6e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Check if table exists to avoid errors on re-run
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    tables = inspector.get_table_names()
    
    if 'settings' not in tables:
        op.create_table('settings',
            sa.Column('key', sa.String(length=255), nullable=False),
            sa.Column('value', sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint('key')
        )


def downgrade() -> None:
    op.drop_table('settings')
