"""Bid on exchanges: budget reservations, pacing, and the exchange bid log.

Revision ID: 20261030_01
Revises: 20260819_01

Campaigns gain reserved_micros (the most their in-flight exchange bids could
cost, counted against both budgets) and pacing. Auction results and
impressions gain source, and lose the requirement of a house slot, which an
impression bought on an exchange does not have. exchange_bids is the ledger
of every bid sent to an exchange and the log the next model trains on.

Every existing row keeps its meaning: nothing is reserved, every campaign
paces asap as it always did, and every existing auction is a house one.
"""

from alembic import op
import sqlalchemy as sa


revision = "20261030_01"
down_revision = "20260819_01"
branch_labels = None
depends_on = None


def _columns(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "campaigns" in tables:
        existing = _columns(inspector, "campaigns")
        if "reserved_micros" not in existing:
            op.add_column("campaigns", sa.Column(
                "reserved_micros", sa.BigInteger(), nullable=False, server_default="0"))
        if "pacing" not in existing:
            op.add_column("campaigns", sa.Column(
                "pacing", sa.String(10), nullable=False, server_default="asap"))

    for table in ("auction_results", "impressions"):
        if table not in tables:
            continue
        if "source" not in _columns(inspector, table):
            op.add_column(table, sa.Column(
                "source", sa.String(10), nullable=False, server_default="house"))
        op.alter_column(table, "ad_slot_id", existing_type=sa.String(36), nullable=True)

    if "exchange_bids" not in tables and "campaigns" in tables:
        op.create_table(
            "exchange_bids",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("request_id", sa.String(64), nullable=False),
            sa.Column("imp_id", sa.String(64), nullable=False),
            sa.Column("campaign_id", sa.String(36), sa.ForeignKey("campaigns.id"), nullable=False),
            sa.Column("creative_id", sa.String(36), sa.ForeignKey("ad_creatives.id"), nullable=False),
            sa.Column("strategy", sa.String(20), nullable=False),
            sa.Column("model_version", sa.String(64), nullable=False),
            sa.Column("pctr", sa.Float(), nullable=False),
            sa.Column("features", sa.Text(), nullable=False),
            sa.Column("bid_cents", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(10), nullable=False),
            sa.Column("reserved_micros", sa.BigInteger(), nullable=False),
            sa.Column("released_micros", sa.BigInteger(), nullable=False),
            sa.Column("charged_micros", sa.BigInteger(), nullable=True),
            sa.Column("impression_id", sa.String(36), nullable=True),
            sa.Column("loss_reason", sa.Integer(), nullable=True),
            sa.Column("clicked", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("settled_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_exchange_bids_status_created", "exchange_bids",
                        ["status", "created_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "exchange_bids" in tables:
        op.drop_index("ix_exchange_bids_status_created", table_name="exchange_bids")
        op.drop_table("exchange_bids")
    for table in ("auction_results", "impressions"):
        if table in tables and "source" in _columns(inspector, table):
            op.drop_column(table, "source")
    if "campaigns" in tables:
        existing = _columns(inspector, "campaigns")
        for column in ("pacing", "reserved_micros"):
            if column in existing:
                op.drop_column("campaigns", column)
