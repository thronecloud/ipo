"""add field provenance

Revision ID: d1f7a3c9e5b2
Revises: 53de03c90075
Create Date: 2026-07-20 08:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd1f7a3c9e5b2'
down_revision: Union[str, Sequence[str], None] = '53de03c90075'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("stocks", sa.Column("sector_source", sa.String(length=16), nullable=True))
    op.add_column("stocks", sa.Column("industry_source", sa.String(length=16), nullable=True))
    op.add_column("stocks", sa.Column("isin_source", sa.String(length=16), nullable=True))
    # Existing non-null values predate provenance tracking. Mark them unknown
    # rather than claiming they were measured.
    op.execute("UPDATE stocks SET sector_source='unknown' WHERE sector IS NOT NULL")
    op.execute("UPDATE stocks SET industry_source='unknown' WHERE industry IS NOT NULL")
    op.execute("UPDATE stocks SET isin_source='unknown' WHERE isin IS NOT NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("stocks", "isin_source")
    op.drop_column("stocks", "industry_source")
    op.drop_column("stocks", "sector_source")
