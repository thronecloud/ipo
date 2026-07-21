"""add exchange direct feeds

corporate_announcements, corporate_filings, bulk_deals, shareholding_patterns —
the tables behind the NSE/BSE direct scrapers and the autonomous report fetcher.
New tables only; nothing to backfill.

Revision ID: c7f1a9d3e820
Revises: e2b5c8a41f96
Create Date: 2026-07-21 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c7f1a9d3e820'
down_revision: Union[str, Sequence[str], None] = 'e2b5c8a41f96'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "corporate_announcements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("exchange", sa.String(length=8), nullable=False),
        sa.Column("headline", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=256), nullable=True),
        sa.Column("attachment_url", sa.String(length=1024), nullable=True),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("announcement_id", sa.String(length=128), nullable=True),
        sa.Column("dedup_hash", sa.String(length=64), nullable=False),
        sa.Column("raw", _JSONB, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("exchange", "dedup_hash", name="uq_corp_ann_exchange_hash"),
    )
    op.create_index("ix_corp_ann_symbol", "corporate_announcements", ["symbol"])
    op.create_index("ix_corp_ann_announced", "corporate_announcements", ["announced_at"])
    op.create_index("ix_corp_ann_stock", "corporate_announcements", ["stock_id"])

    op.create_table(
        "corporate_filings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("filing_type", sa.String(length=64), nullable=True),
        sa.Column("period", sa.String(length=64), nullable=True),
        sa.Column("source_url", sa.String(length=1024), nullable=False),
        sa.Column("local_path", sa.String(length=512), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id", "source_url", name="uq_filing_stock_url"),
    )
    op.create_index("ix_filing_stock", "corporate_filings", ["stock_id"])
    op.create_index("ix_filing_sha256", "corporate_filings", ["sha256"])

    op.create_table(
        "bulk_deals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("exchange", sa.String(length=8), nullable=False),
        sa.Column("deal_date", sa.Date(), nullable=True),
        sa.Column("client_name", sa.String(length=512), nullable=True),
        sa.Column("buy_sell", sa.String(length=8), nullable=True),
        sa.Column("quantity", sa.BigInteger(), nullable=True),
        sa.Column("avg_price", sa.Float(), nullable=True),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("dedup_hash", sa.String(length=64), nullable=False),
        sa.Column("raw", _JSONB, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedup_hash", name="uq_bulk_deal_hash"),
    )
    op.create_index("ix_bulk_deal_symbol", "bulk_deals", ["symbol"])
    op.create_index("ix_bulk_deal_date", "bulk_deals", ["deal_date"])
    op.create_index("ix_bulk_deal_stock", "bulk_deals", ["stock_id"])

    op.create_table(
        "shareholding_patterns",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("promoter_pct", sa.Float(), nullable=True),
        sa.Column("fii_pct", sa.Float(), nullable=True),
        sa.Column("dii_pct", sa.Float(), nullable=True),
        sa.Column("public_pct", sa.Float(), nullable=True),
        sa.Column("pledged_pct", sa.Float(), nullable=True),
        sa.Column("raw", _JSONB, nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stock_id", "period_end", name="uq_shp_stock_period"),
    )
    op.create_index("ix_shp_stock_period", "shareholding_patterns", ["stock_id", "period_end"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("shareholding_patterns")
    op.drop_index("ix_bulk_deal_stock", table_name="bulk_deals")
    op.drop_index("ix_bulk_deal_date", table_name="bulk_deals")
    op.drop_index("ix_bulk_deal_symbol", table_name="bulk_deals")
    op.drop_table("bulk_deals")
    op.drop_index("ix_filing_sha256", table_name="corporate_filings")
    op.drop_index("ix_filing_stock", table_name="corporate_filings")
    op.drop_table("corporate_filings")
    op.drop_index("ix_corp_ann_stock", table_name="corporate_announcements")
    op.drop_index("ix_corp_ann_announced", table_name="corporate_announcements")
    op.drop_index("ix_corp_ann_symbol", table_name="corporate_announcements")
    op.drop_table("corporate_announcements")
