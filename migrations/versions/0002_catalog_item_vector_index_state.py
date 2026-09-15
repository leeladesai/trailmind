"""catalog item vector index state

P0-1: explicit per-item reconciliation state (pending/synced/failed) instead of a
single optimistic `vector_synced` boolean — see
app/services/catalog_reconciliation.py. `server_default` values are required here
(not just a Python-side model default) so this ADD COLUMN succeeds against a
table that already has rows, on both SQLite and Postgres.

Revision ID: ca95f6509eea
Revises: a4bb30b5eed6
Create Date: 2026-09-13 08:21:55.318512

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "ca95f6509eea"
down_revision: Union[str, None] = "a4bb30b5eed6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "catalog_items",
        sa.Column(
            "vector_index_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "catalog_items", sa.Column("vector_index_error", sa.Text(), nullable=True)
    )
    op.add_column(
        "catalog_items",
        sa.Column("vector_indexed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "catalog_items",
        sa.Column(
            "vector_index_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("catalog_items", "vector_index_attempts")
    op.drop_column("catalog_items", "vector_indexed_at")
    op.drop_column("catalog_items", "vector_index_error")
    op.drop_column("catalog_items", "vector_index_status")
