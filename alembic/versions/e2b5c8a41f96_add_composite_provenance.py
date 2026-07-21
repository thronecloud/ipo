"""add composite provenance

Revision ID: e2b5c8a41f96
Revises: d1f7a3c9e5b2
Create Date: 2026-07-21 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e2b5c8a41f96'
down_revision: Union[str, Sequence[str], None] = 'd1f7a3c9e5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

# The contributing analyses of a stock's composite: the latest per persona that
# carried a score — the exact selection recompute_scores_for_stock uses. A NULL
# prompt_version/model predates provenance tracking and is counted as 'unknown'
# rather than dropped.
_LATEST = """
    SELECT DISTINCT ON (a.stock_id, a.persona)
           a.stock_id AS stock_id,
           COALESCE(a.prompt_version, 'unknown') AS prompt_version,
           COALESCE(a.model, 'unknown') AS model
    FROM analyses a
    WHERE a.score IS NOT NULL
    ORDER BY a.stock_id, a.persona, a.analyzed_at DESC, a.id DESC
"""


def _backfill(table: str, column: str, field: str) -> str:
    return f"""
    WITH latest AS ({_LATEST}),
    counts AS (
        SELECT stock_id, {field} AS val, count(*) AS n
        FROM latest GROUP BY stock_id, {field}
    ),
    agg AS (
        SELECT stock_id, jsonb_object_agg(val, n) AS obj
        FROM counts GROUP BY stock_id
    )
    UPDATE {table} t SET {column} = agg.obj
    FROM agg WHERE agg.stock_id = t.stock_id
    """


def upgrade() -> None:
    """Upgrade schema."""
    for table in ("composite_scores", "composite_score_history"):
        op.add_column(table, sa.Column("prompt_versions", _JSONB, nullable=True))
        op.add_column(table, sa.Column("models_used", _JSONB, nullable=True))
        op.execute(_backfill(table, "prompt_versions", "prompt_version"))
        op.execute(_backfill(table, "models_used", "model"))


def downgrade() -> None:
    """Downgrade schema."""
    for table in ("composite_score_history", "composite_scores"):
        op.drop_column(table, "models_used")
        op.drop_column(table, "prompt_versions")
