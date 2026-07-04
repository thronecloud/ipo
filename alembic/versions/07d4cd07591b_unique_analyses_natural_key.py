"""unique analyses natural key

P4-X1: dedupe analyses on (stock_id, persona, data_hash, model) keeping the
newest row (max id), then enforce the natural key with a UNIQUE constraint.
NULL data_hash/model rows never conflict (Postgres NULLs are distinct), so the
dedup only touches rows that would violate the new constraint.

Revision ID: 07d4cd07591b
Revises: 35f5b162beb2
Create Date: 2026-07-04 15:40:11.494230

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07d4cd07591b'
down_revision: Union[str, Sequence[str], None] = '35f5b162beb2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Keep the newest duplicate (highest id = latest verdict); delete the rest.
    # stock_insights.source_analysis_id FK is ON DELETE SET NULL — safe.
    op.execute(
        """
        DELETE FROM analyses a
        USING analyses b
        WHERE a.stock_id = b.stock_id
          AND a.persona = b.persona
          AND a.data_hash = b.data_hash
          AND a.model = b.model
          AND a.id < b.id
        """
    )
    op.create_unique_constraint(
        "uq_analysis_natural_key",
        "analyses",
        ["stock_id", "persona", "data_hash", "model"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint("uq_analysis_natural_key", "analyses", type_="unique")
