"""add raw_payloads bitemporal archive index

raw_payloads — one row per distinct fetched payload, keyed by the sha256 of its raw
(uncompressed) bytes. The ingest seams archive the compressed payload to disk BEFORE
parsing and record its location here, so a parser bug is replayable and the past
state of a source is reconstructable. sha256 is UNIQUE (identical content is stored
once); `path` is NULLed with a `pruned_at` stamp when the retention job deletes the
file, so the record of what existed outlives the bytes.

A new, empty table; nothing to backfill.

Revision ID: a1c93f7e2d84
Revises: 55233f9342f8
Create Date: 2026-07-21 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c93f7e2d84'
down_revision: Union[str, Sequence[str], None] = '55233f9342f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "raw_payloads",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("entity", sa.String(length=128), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_type", sa.String(length=64), nullable=True),
        sa.Column("pruned_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256", name="uq_raw_payload_sha256"),
    )
    op.create_index("ix_raw_payload_source_entity", "raw_payloads", ["source", "entity"])
    op.create_index("ix_raw_payload_source_fetched", "raw_payloads", ["source", "fetched_at"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_raw_payload_source_fetched", table_name="raw_payloads")
    op.drop_index("ix_raw_payload_source_entity", table_name="raw_payloads")
    op.drop_table("raw_payloads")
