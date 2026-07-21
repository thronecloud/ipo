"""add source_discrepancies and source_trust

source_discrepancies — one row per open (or resolved) disagreement between two sources
about the same fact (price bar, market cap, promoter holding), deduped on
(stock_id, fact, fact_key). The cross-source reconciler writes it; correctness never
mutates source data, so this table IS the flag.
source_trust — the per-source-per-fact rolling agreement rate, one materialized row per
(source, fact), recomputed by the reconcile job.

Both are new, empty tables; nothing to backfill.

Revision ID: 55233f9342f8
Revises: f2a8d1c40b73
Create Date: 2026-07-21 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '55233f9342f8'
down_revision: Union[str, Sequence[str], None] = 'f2a8d1c40b73'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "source_discrepancies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("fact", sa.String(length=32), nullable=False),
        sa.Column("fact_key", sa.String(length=32), nullable=False),
        sa.Column("source_a", sa.String(length=32), nullable=False),
        sa.Column("value_a", sa.Float(), nullable=True),
        sa.Column("source_b", sa.String(length=32), nullable=False),
        sa.Column("value_b", sa.Float(), nullable=True),
        sa.Column("divergence_pct", sa.Float(), nullable=True),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id", "fact", "fact_key",
                            name="uq_source_disc_stock_fact_key"),
    )
    op.create_index("ix_source_disc_stock", "source_discrepancies", ["stock_id"])
    op.create_index("ix_source_disc_open", "source_discrepancies", ["resolved_at"])
    op.create_index("ix_source_disc_detected", "source_discrepancies", ["detected_at"])

    op.create_table(
        "source_trust",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("fact", sa.String(length=32), nullable=False),
        sa.Column("agreements", sa.Integer(), nullable=False),
        sa.Column("comparisons", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "fact", name="uq_source_trust_source_fact"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("source_trust")
    op.drop_index("ix_source_disc_detected", table_name="source_discrepancies")
    op.drop_index("ix_source_disc_open", table_name="source_discrepancies")
    op.drop_index("ix_source_disc_stock", table_name="source_discrepancies")
    op.drop_table("source_discrepancies")
