"""A page URL is classified into a content category, so any URL means something.

Domain allow lists can only match what somebody enumerated in advance, which
makes every unlisted domain behave identically. Categories are how real RTB
solves this: the advertiser buys a category and the exchange says what the page
is. These tests pin the parts of that inference which are easy to get subtly
backwards.
"""

import pytest

from app.services.contextual import classify_url, domain_of


def category(url):
    return classify_url(url)[0]


def source(url):
    return classify_url(url)[1]


# ── the recognisable-domain table ────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://espn.com/nfl/story", "sports"),
    ("https://bloomberg.com/quote/AAPL", "finance"),
    ("https://vogue.com/article", "fashion"),
    ("https://techcrunch.com/2024/01/a-startup", "tech"),
    ("https://punchng.com/some-story", "news"),
    ("https://imdb.com/title/tt1", "entertainment"),
])
def test_known_domains_are_classified(url, expected):
    assert category(url) == expected
    assert source(url) == "known_domain"


def test_a_subdomain_inherits_its_parent_domain():
    assert category("https://edition.cnn.com/2024/story") == "news"


# ── sections beat the masthead ───────────────────────────────────────────────

def test_a_section_path_overrides_the_sites_own_category():
    """bbc.com is news, but bbc.com/sport is sports inventory."""
    assert classify_url("https://bbc.com/sport/football") == ("sports", "section")
    assert classify_url("https://bbc.com/news/world") == ("news", "section")


def test_only_the_first_path_segment_counts_as_a_section():
    """Otherwise a TechCrunch story about funding reads as finance."""
    assert classify_url(
        "https://techcrunch.com/2024/01/money-raised"
    ) == ("tech", "known_domain")


# ── keyword inference for everything else ────────────────────────────────────

def test_an_unlisted_domain_is_inferred_from_its_name():
    assert classify_url("https://nigeriasportsdaily.com/x") == ("sports", "keyword")


def test_a_leading_word_wins_over_one_buried_mid_name():
    """'newsportal' contains 'sport'. It is a news portal.

    This is the pair a plain substring scan gets backwards, so both directions
    are pinned here.
    """
    assert category("https://newsportal.co/x") == "news"
    assert category("https://sportsdailynews.com/x") == "sports"


def test_a_subdomain_can_carry_the_signal():
    assert category("https://sports.example.com/x") == "sports"


def test_a_meaningless_tld_is_not_read_as_a_category():
    """A '.news' suffix says nothing about the page."""
    assert category("https://mybrand.news/x") is None


# ── refusing to guess ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://google.com/",          # a search engine is not a content category
    "https://example.com/",
    "garbage",
    "",
])
def test_an_unclassifiable_url_returns_nothing_rather_than_a_guess(url):
    assert classify_url(url) == (None, "unknown")


# ── domain extraction lives here now ─────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://www.Example.COM:8443/x", "example.com"),
    ("example.com/x", "example.com"),          # publishers omit the scheme
    ("not a url", None),
])
def test_domain_extraction(url, expected):
    assert domain_of(url) == expected
