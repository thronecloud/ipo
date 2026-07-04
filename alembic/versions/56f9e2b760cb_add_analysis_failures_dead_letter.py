"""add analysis failures dead letter

P4: persistent dead-letter ledger for persona analysis. One row per failing
(stock, persona, data_hash); find_work skips pairs at/over the engine threshold
so repeated failures stop re-burning the daily analysis cap.

Revision ID: 56f9e2b760cb
Revises: 07d4cd07591b
Create Date: 2026-07-04 15:48:14.111497

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '56f9e2b760cb'
down_revision: Union[str, Sequence[str], None] = '07d4cd07591b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analysis_failures",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("persona", sa.String(length=64), nullable=False),
        sa.Column("data_hash", sa.String(length=64), nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id", "persona", "data_hash",
                            name="uq_analysis_failure_key"),
    )
    op.create_index(op.f("ix_analysis_failures_stock_id"), "analysis_failures",
                    ["stock_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_analysis_failures_stock_id"), table_name="analysis_failures")
    op.drop_table("analysis_failures")
