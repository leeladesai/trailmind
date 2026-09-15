"""recommendation mesh audit fields

Revision ID: d8fcbff178e4
Revises: e9428313a28b
Create Date: 2026-09-13 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d8fcbff178e4"
down_revision: Union[str, None] = "e9428313a28b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "recommendations", sa.Column("mesh_model", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "recommendations", sa.Column("mesh_raw_prompt", sa.Text(), nullable=True)
    )
    op.add_column(
        "recommendations", sa.Column("mesh_raw_response", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("recommendations", "mesh_raw_response")
    op.drop_column("recommendations", "mesh_raw_prompt")
    op.drop_column("recommendations", "mesh_model")
