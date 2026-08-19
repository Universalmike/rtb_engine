"""Give campaigns domain targeting and a brand-safety block list.

Revision ID: 20260819_01
Revises: 20260818_01

page_url was accepted on every bid request and read nowhere. These two columns
are what make it mean something. Both default to an empty list, which the
auction reads as "no restriction", so campaigns that already exist keep bidding
exactly as they did before this ran.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260819_01"
down_revision = "20260818_01"
branch_labels = None
depends_on = None

COLUMNS = ("target_domains", "blocked_domains")


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "campaigns" not in inspector.get_table_names():
        return
    existing = _column_names(inspector, "campaigns")
    for column in COLUMNS:
        if column not in existing:
            op.add_column(
                "campaigns",
                sa.Column(
                    column, sa.Text(), nullable=False, server_default="[]"
                ),
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "campaigns" not in inspector.get_table_names():
        return
    existing = _column_names(inspector, "campaigns")
    for column in COLUMNS:
        if column in existing:
            op.drop_column("campaigns", column)
