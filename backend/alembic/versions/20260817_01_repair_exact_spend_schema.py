"""Repair incomplete exact-spend schema upgrades.

Revision ID: 20260817_01
Revises: 20260812_01

Some existing demo databases were marked at ``20260812_01`` without all of
that revision's DDL being applied.  A follow-up revision is intentional: it
still runs when Alembic believes the original migration is current, and every
operation below is guarded so fully migrated databases remain unchanged.
"""

from alembic import op
import sqlalchemy as sa


revision = "20260817_01"
down_revision = "20260812_01"
branch_labels = None
depends_on = None

MICROS_PER_CPM_CENT = 10


def _column_names(inspector: sa.Inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    required_tables = {"campaigns", "auction_results", "impressions"}
    if not required_tables.issubset(inspector.get_table_names()):
        # Keep fresh/partially-created demo databases recoverable. create_all
        # creates only missing tables; the guarded DDL below repairs existing
        # tables without dropping data.
        from app.core.database import Base
        import app.models.models  # noqa: F401

        Base.metadata.create_all(bind)
        inspector = sa.inspect(bind)

    campaign_columns = _column_names(inspector, "campaigns")
    auction_columns = _column_names(inspector, "auction_results")
    impression_columns = _column_names(inspector, "impressions")

    if "spent_today_micros" not in campaign_columns:
        op.add_column(
            "campaigns",
            sa.Column(
                "spent_today_micros",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        )

    if "total_spent_micros" not in campaign_columns:
        op.add_column(
            "campaigns",
            sa.Column(
                "total_spent_micros",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        )

    if "spend_date" not in campaign_columns:
        op.add_column(
            "campaigns",
            sa.Column(
                "spend_date",
                sa.Date(),
                nullable=False,
                server_default=sa.text("CURRENT_DATE"),
            ),
        )

    if "charged_cost_micros" not in auction_columns:
        op.add_column(
            "auction_results",
            sa.Column(
                "charged_cost_micros",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        )

    if "charged_cost_micros" not in impression_columns:
        op.add_column(
            "impressions",
            sa.Column(
                "charged_cost_micros",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            ),
        )

    # Repair nullable or zero-valued rows left by an interrupted/manual schema
    # change. A positive clearing CPM always has a positive microdollar charge.
    op.execute(
        f"""
        UPDATE auction_results
        SET charged_cost_micros =
            CAST(clearing_price_cents AS BIGINT) * {MICROS_PER_CPM_CENT}
        WHERE charged_cost_micros IS NULL
           OR (charged_cost_micros = 0 AND clearing_price_cents > 0)
        """
    )
    op.execute(
        f"""
        UPDATE impressions
        SET charged_cost_micros =
            CAST(clearing_price_cents AS BIGINT) * {MICROS_PER_CPM_CENT}
        WHERE charged_cost_micros IS NULL
           OR (charged_cost_micros = 0 AND clearing_price_cents > 0)
        """
    )

    op.execute(
        "UPDATE campaigns SET spend_date = CURRENT_DATE WHERE spend_date IS NULL"
    )

    # Impression rows are the billing ledger, so rebuild both campaign totals
    # from them rather than converting the legacy over-counted cent counters.
    op.execute(
        """
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
        """
    )

    if "spent_today_cents" in campaign_columns:
        op.drop_column("campaigns", "spent_today_cents")
    if "total_spent_cents" in campaign_columns:
        op.drop_column("campaigns", "total_spent_cents")

    op.alter_column(
        "campaigns",
        "spent_today_micros",
        existing_type=sa.BigInteger(),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "campaigns",
        "total_spent_micros",
        existing_type=sa.BigInteger(),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "campaigns",
        "spend_date",
        existing_type=sa.Date(),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "auction_results",
        "charged_cost_micros",
        existing_type=sa.BigInteger(),
        nullable=False,
        server_default=None,
    )
    op.alter_column(
        "impressions",
        "charged_cost_micros",
        existing_type=sa.BigInteger(),
        nullable=False,
        server_default=None,
    )


def downgrade() -> None:
    # This revision repairs the schema promised by its parent revision.
    # Downgrading to 20260812_01 must therefore retain that schema.
    pass
