"""add document extraction

extracted_documents + extracted_financials — the tables behind the deterministic,
OCR-capable document-extraction pipeline. New tables only; nothing to backfill.

Revision ID: d4b1f7e0a9c2
Revises: c7f1a9d3e820
Create Date: 2026-07-21 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd4b1f7e0a9c2'
down_revision: Union[str, Sequence[str], None] = 'c7f1a9d3e820'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "extracted_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("method_summary", _JSONB, nullable=True),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("pages_ocr", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("text_path", sa.String(length=512), nullable=True),
        sa.ForeignKeyConstraint(["filing_id"], ["corporate_filings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("filing_id", name="uq_extracted_doc_filing"),
    )
    op.create_index("ix_extracted_doc_stock", "extracted_documents", ["stock_id"])

    op.create_table(
        "extracted_financials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filing_id", sa.Integer(), nullable=False),
        sa.Column("stock_id", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("raw_label", sa.String(length=512), nullable=True),
        sa.Column("period_label", sa.String(length=128), nullable=True),
        sa.Column("value_cr", sa.Float(), nullable=True),
        sa.Column("unit_detected", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["filing_id"], ["corporate_filings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stock_id"], ["stocks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extracted_fin_filing", "extracted_financials", ["filing_id"])
    op.create_index("ix_extracted_fin_stock", "extracted_financials", ["stock_id"])
    op.create_index("ix_extracted_fin_metric", "extracted_financials", ["metric"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_extracted_fin_metric", table_name="extracted_financials")
    op.drop_index("ix_extracted_fin_stock", table_name="extracted_financials")
    op.drop_index("ix_extracted_fin_filing", table_name="extracted_financials")
    op.drop_table("extracted_financials")
    op.drop_index("ix_extracted_doc_stock", table_name="extracted_documents")
    op.drop_table("extracted_documents")
