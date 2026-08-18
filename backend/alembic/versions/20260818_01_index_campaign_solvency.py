"""Index the columns the auction's solvency query filters on.

Revision ID: 20260818_01
Revises: 20260817_01

Every bid request opens by selecting campaigns on ``status`` and comparing
``spend_date`` to today. The existing ``ix_campaigns_advertiser_status`` index
cannot serve that query: ``advertiser_id`` leads it and the auction never
filters on an advertiser. Guarded so it is safe to re-run.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260818_01"
down_revision = "20260817_01"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_campaigns_status_spend_date"


def _index_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "campaigns" not in inspector.get_table_names():
        return
    if INDEX_NAME not in _index_names(inspector, "campaigns"):
        op.create_index(INDEX_NAME, "campaigns", ["status", "spend_date"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "campaigns" not in inspector.get_table_names():
        return
    if INDEX_NAME in _index_names(inspector, "campaigns"):
        op.drop_index(INDEX_NAME, table_name="campaigns")
