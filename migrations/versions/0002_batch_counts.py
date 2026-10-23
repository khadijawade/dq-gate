"""store ingest counts on batches

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

COLUMNS = ("inserted_count", "duplicate_count", "invalid_count")


def upgrade() -> None:
    # Nullable: batches loaded before this migration never had their counts recorded, and
    # NULL says "unknown" where 0 would be a false claim.
    for name in COLUMNS:
        op.add_column("batches", sa.Column(name, sa.Integer, nullable=True))


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("batches", name)
