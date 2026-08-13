from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.models.models import Campaign, CampaignStatus
from app.schemas.schemas import CampaignCreate
from app.services.accounting import (
    MICROS_PER_CENT,
    cpm_cents_to_impression_micros,
    max_affordable_cpm_cents,
    micros_to_cents,
)


def test_cpm_is_converted_to_one_impression_cost_exactly():
    assert cpm_cents_to_impression_micros(150) == 1_500
    assert cpm_cents_to_impression_micros(1) == 10
    assert micros_to_cents(1_500) == 0.15


def test_negative_cpm_is_rejected():
    with pytest.raises(ValueError):
        cpm_cents_to_impression_micros(-1)


def test_affordable_cpm_uses_both_fractional_cent_precision_and_flooring():
    assert max_affordable_cpm_cents(MICROS_PER_CENT) == 1_000
    assert max_affordable_cpm_cents(1_509) == 150
    assert max_affordable_cpm_cents(9) == 0


def test_campaign_reports_micro_spend_as_cents_and_enforces_total_budget():
    campaign = Campaign(
        daily_budget_cents=500,
        total_budget_cents=1_000,
        spent_today_micros=12_345,
        total_spent_micros=10_000_000,
        spend_date=date.today(),
        status=CampaignStatus.ACTIVE,
    )

    assert campaign.spent_today_cents == 1.2345
    assert campaign.total_spent_cents == 1_000
    assert campaign.remaining_daily_budget_micros == 4_987_655
    assert campaign.remaining_total_budget_micros == 0
    assert campaign.can_bid is False


def _campaign_payload(**overrides):
    now = datetime.utcnow()
    payload = {
        "advertiser_id": "advertiser-1",
        "name": "Campaign",
        "daily_budget_cents": 100,
        "total_budget_cents": 1_000,
        "max_cpm_cents": 150,
        "start_date": now,
        "end_date": now + timedelta(days=1),
    }
    payload.update(overrides)
    return payload


def test_campaign_rejects_total_budget_below_daily_budget():
    with pytest.raises(ValidationError, match="total_budget_cents"):
        CampaignCreate(**_campaign_payload(total_budget_cents=99))


def test_campaign_rejects_end_before_start():
    now = datetime.utcnow()
    with pytest.raises(ValidationError, match="end_date"):
        CampaignCreate(**_campaign_payload(
            start_date=now,
            end_date=now - timedelta(seconds=1),
        ))
