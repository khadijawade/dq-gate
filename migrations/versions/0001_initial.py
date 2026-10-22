"""batches, organizations, events

Revision ID: 0001
Revises:
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("expected_count", sa.Integer, nullable=False),
        sa.Column("received_count", sa.Integer, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('accepted', 'rejected')", name="batches_status_check"),
    )

    op.create_table(
        "organizations",
        sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
        ),
        sa.Column("fingerprint", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("country", sa.Text, nullable=False),
        sa.Column("website", sa.Text, nullable=True),
        sa.Column("batch_id", sa.BigInteger, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    # One live row per fingerprint; archived rows keep their history without blocking re-entry.
    op.create_index(
        "organizations_fingerprint_live_uniq",
        "organizations",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("archived_at IS NULL"),
    )

    op.create_table(
        "events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # Not a foreign key on purpose: the organization may live in another service.
        sa.Column("org_ref", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("starts_on", sa.Date, nullable=False),
        sa.Column("date_precision", sa.Text, nullable=False),
        sa.Column("batch_id", sa.BigInteger, sa.ForeignKey("batches.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "date_precision IN ('day', 'month', 'year')", name="events_date_precision_check"
        ),
    )
    op.create_index("events_org_ref_idx", "events", ["org_ref"])


def downgrade() -> None:
    op.drop_index("events_org_ref_idx", table_name="events")
    op.drop_table("events")
    op.drop_index("organizations_fingerprint_live_uniq", table_name="organizations")
    op.drop_table("organizations")
    op.drop_table("batches")
