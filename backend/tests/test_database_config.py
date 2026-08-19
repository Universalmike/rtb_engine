"""Prepared-statement caching is decided by the endpoint, not the vendor name.

Disabling the cache is not free: without it Postgres re-parses and re-plans
every query on every request. It must therefore be disabled only where it is
genuinely unsafe -- against a transaction-mode pooler, which hands a different
backend connection to each transaction.
"""

import pytest

from app.core.database import uses_transaction_pooler

SUPABASE_DIRECT = "postgresql://u:p@db.abcdefgh.supabase.co:5432/postgres"
SUPABASE_SESSION = "postgresql://u:p@aws-0-eu-west-2.pooler.supabase.com:5432/postgres"
SUPABASE_TRANSACTION = "postgresql://u:p@aws-0-eu-west-2.pooler.supabase.com:6543/postgres"
NEON_DIRECT = "postgresql://u:p@ep-cool-name-123456.eu-central-1.aws.neon.tech/db"
NEON_POOLED = "postgresql://u:p@ep-cool-name-123456-pooler.eu-central-1.aws.neon.tech/db"
LOCAL = "postgresql://postgres:postgres@localhost:5432/rtb"


@pytest.mark.parametrize("url", [SUPABASE_TRANSACTION, NEON_POOLED])
def test_transaction_poolers_must_not_cache_statements(url):
    assert uses_transaction_pooler(url) is True


@pytest.mark.parametrize(
    "url", [SUPABASE_DIRECT, SUPABASE_SESSION, NEON_DIRECT, LOCAL]
)
def test_direct_and_session_endpoints_may_cache_statements(url):
    assert uses_transaction_pooler(url) is False


def test_supabase_pooler_is_split_by_port_not_hostname():
    """Session and transaction poolers share a host; only the port differs."""
    assert uses_transaction_pooler(SUPABASE_SESSION) is False
    assert uses_transaction_pooler(SUPABASE_TRANSACTION) is True


def test_the_old_vendor_name_heuristic_would_have_been_wrong():
    """Pins the regression: 'supabase' in the URL is true for all three."""
    for url in (SUPABASE_DIRECT, SUPABASE_SESSION, SUPABASE_TRANSACTION):
        assert "supabase" in url
    assert uses_transaction_pooler(SUPABASE_DIRECT) is False


def test_an_unparseable_url_fails_safe():
    """Wrongly caching against a pooler is a runtime error; being slow is not."""
    assert uses_transaction_pooler("postgresql://u:p@host:notaport/db") is True
