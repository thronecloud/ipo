"""add corporate_events and analysis_queue

corporate_events — the upcoming board-meeting / results calendar off NSE's
event-calendar feed, the trigger source for event-driven fetching.
analysis_queue — the pending-analysis signal the event trigger writes and the analyze
job consumes (queued stocks first, oldest-first).

Both are new, empty tables; nothing to backfill.

Revision ID: f2a8d1c40b73
Revises: d4b1f7e0a9c2
Create Date: 2026-07-21 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f2a8d1c40b73'
down_revision: Union[str, Sequence[str], None] = 'd4b1f7e0a9c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "corporate_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("purpose_text", sa.Text(), nullable=True),
        sa.Column("purpose_hash", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("chased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw", _JSONB, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol", "event_date", "purpose_hash",
                            name="uq_corp_event_symbol_date_purpose"),
    )
    op.create_index("ix_corp_event_symbol", "corporate_events", ["symbol"])
    op.create_index("ix_corp_event_date", "corporate_events", ["event_date"])
    op.create_index("ix_corp_event_stock", "corporate_events", ["stock_id"])

    op.create_table(
        "analysis_queue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_analysis_queue_pending", "analysis_queue",
                    ["consumed_at", "queued_at"])
    op.create_index("ix_analysis_queue_stock", "analysis_queue", ["stock_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_analysis_queue_stock", table_name="analysis_queue")
    op.drop_index("ix_analysis_queue_pending", table_name="analysis_queue")
    op.drop_table("analysis_queue")
    op.drop_index("ix_corp_event_stock", table_name="corporate_events")
    op.drop_index("ix_corp_event_date", table_name="corporate_events")
    op.drop_index("ix_corp_event_symbol", table_name="corporate_events")
    op.drop_table("corporate_events")
