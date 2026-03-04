"""add transcript_source to books

Revision ID: f6b2c4d8e9a1
Revises: e4a1c2d9f7b3
Create Date: 2026-02-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f6b2c4d8e9a1"
down_revision: Union[str, Sequence[str], None] = "e4a1c2d9f7b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("books")}
    if "transcript_source" not in columns:
        op.add_column("books", sa.Column("transcript_source", sa.String(length=32), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("books")}
    if "transcript_source" in columns:
        op.drop_column("books", "transcript_source")
