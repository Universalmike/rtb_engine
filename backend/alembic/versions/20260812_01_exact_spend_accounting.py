"""Store exact per-impression spend in microdollars.

Revision ID: 20260812_01
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "20260812_01"
down_revision = None
branch_labels = None
depends_on = None

MICROS_PER_CPM_CENT = 10


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # This repository previously relied on create_all and therefore has no
    # baseline migration. A new database can safely be created from current
    # metadata; existing demo databases follow the data-preserving path below.
    if "campaigns" not in inspector.get_table_names():
        from app.core.database import Base
        import app.models.models  # noqa: F401

        Base.metadata.create_all(bind)
        return

    campaign_columns = {c["name"] for c in inspector.get_columns("campaigns")}
    auction_columns = {c["name"] for c in inspector.get_columns("auction_results")}
    impression_columns = {c["name"] for c in inspector.get_columns("impressions")}

    if "spent_today_micros" not in campaign_columns:
        op.add_column(
            "campaigns",
            sa.Column("spent_today_micros", sa.BigInteger(), nullable=False,
                      server_default="0"),
        )
        op.add_column(
            "campaigns",
            sa.Column("total_spent_micros", sa.BigInteger(), nullable=False,
                      server_default="0"),
        )

    if "spend_date" not in campaign_columns:
        op.add_column(
            "campaigns",
            sa.Column("spend_date", sa.Date(), nullable=False,
                      server_default=sa.text("CURRENT_DATE")),
        )

    if "charged_cost_micros" not in auction_columns:
        op.add_column(
            "auction_results",
            sa.Column("charged_cost_micros", sa.BigInteger(), nullable=False,
                      server_default="0"),
        )
        op.execute(
            f"UPDATE auction_results SET charged_cost_micros = "
            f"clearing_price_cents * {MICROS_PER_CPM_CENT}"
        )

    if "charged_cost_micros" not in impression_columns:
        op.add_column(
            "impressions",
            sa.Column("charged_cost_micros", sa.BigInteger(), nullable=False,
                      server_default="0"),
        )
        op.execute(
            f"UPDATE impressions SET charged_cost_micros = "
            f"clearing_price_cents * {MICROS_PER_CPM_CENT}"
        )

    # Rebuild spend from billable impression records. The legacy campaign
    # counters charged one entire CPM quote per impression and cannot be safely
    # converted directly.
    op.execute("""
        UPDATE campaigns AS c
        SET total_spent_micros = COALESCE((
                SELECT SUM(i.charged_cost_micros)
                FROM impressions AS i
                WHERE i.campaign_id = c.id
            ), 0),
            spent_today_micros = COALESCE((
                SELECT SUM(i.charged_cost_micros)
                FROM impressions AS i
                WHERE i.campaign_id = c.id
                  AND i.created_at >= CURRENT_DATE
            ), 0)
    """)

    if "spent_today_cents" in campaign_columns:
        op.drop_column("campaigns", "spent_today_cents")
    if "total_spent_cents" in campaign_columns:
        op.drop_column("campaigns", "total_spent_cents")

    op.alter_column("campaigns", "spent_today_micros", server_default=None)
    op.alter_column("campaigns", "total_spent_micros", server_default=None)
    op.alter_column("campaigns", "spend_date", server_default=None)
    op.alter_column("auction_results", "charged_cost_micros", server_default=None)
    op.alter_column("impressions", "charged_cost_micros", server_default=None)


def downgrade() -> None:
    op.add_column(
        "campaigns",
        sa.Column("spent_today_cents", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    op.add_column(
        "campaigns",
        sa.Column("total_spent_cents", sa.Integer(), nullable=False,
                  server_default="0"),
    )
    op.execute(
        "UPDATE campaigns SET "
        "spent_today_cents = spent_today_micros / 10000, "
        "total_spent_cents = total_spent_micros / 10000"
    )
    op.drop_column("impressions", "charged_cost_micros")
    op.drop_column("auction_results", "charged_cost_micros")
    op.drop_column("campaigns", "total_spent_micros")
    op.drop_column("campaigns", "spent_today_micros")
    op.drop_column("campaigns", "spend_date")
