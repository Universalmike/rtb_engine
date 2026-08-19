"""Infer the content category of a page from its URL.

An advertiser buying "sports inventory" does not enumerate every sports site
in the world; they buy a category, and the exchange tells them what the page
is. OpenRTB carries both `site.page` and `site.cat` for exactly this reason.
Without it, targeting can only match domains somebody listed in advance, so
every unlisted domain behaves identically and the page URL may as well be
ignored.

This is a heuristic, not a crawler. Fetching an arbitrary URL mid-auction is
out of the question: the whole latency budget is a fraction of what a request
to an unknown host costs, and it would have the auction reaching out to
caller-supplied addresses. Real contextual providers classify crawled content
offline; reading the URL is the honest approximation available in-process.

Three layers, most confident first:

1. a table of recognisable domains
2. keyword rules over the host labels and path, prefix matches first
3. nothing, and the caller falls back to the publisher's own category

`classify_url` returns the category and which layer produced it, so the answer
can be shown to a caller rather than silently assumed.
"""

from typing import Optional
from urllib.parse import urlsplit

# The categories campaigns already target. Kept in step with the seeder.
CATEGORIES = ("tech", "finance", "sports", "entertainment", "news", "fashion")

# Suffix labels that carry no meaning on their own.
_TLDS = {
    "com", "org", "net", "co", "io", "ng", "uk", "us", "gov", "edu", "info",
    "biz", "tv", "me", "ly", "app", "dev", "news", "xyz", "africa", "za",
    "ke", "gh", "de", "fr", "es", "it", "br", "ca", "au", "in", "jp",
}

KNOWN_DOMAINS = {
    # tech
    "techcrunch.com": "tech", "theverge.com": "tech", "arstechnica.com": "tech",
    "wired.com": "tech", "engadget.com": "tech", "cnet.com": "tech",
    "zdnet.com": "tech", "gizmodo.com": "tech", "github.com": "tech",
    "stackoverflow.com": "tech", "news.ycombinator.com": "tech",
    "tomshardware.com": "tech", "anandtech.com": "tech", "macrumors.com": "tech",
    "9to5mac.com": "tech", "androidauthority.com": "tech", "slashdot.org": "tech",
    "producthunt.com": "tech", "xda-developers.com": "tech",
    # finance
    "bloomberg.com": "finance", "ft.com": "finance", "wsj.com": "finance",
    "cnbc.com": "finance", "marketwatch.com": "finance", "forbes.com": "finance",
    "investopedia.com": "finance", "fortune.com": "finance",
    "economist.com": "finance", "morningstar.com": "finance",
    "seekingalpha.com": "finance", "coindesk.com": "finance",
    "cointelegraph.com": "finance", "nairametrics.com": "finance",
    "businessday.ng": "finance",
    # sports
    "espn.com": "sports", "skysports.com": "sports", "goal.com": "sports",
    "bleacherreport.com": "sports", "cbssports.com": "sports",
    "foxsports.com": "sports", "nba.com": "sports", "nfl.com": "sports",
    "fifa.com": "sports", "uefa.com": "sports", "premierleague.com": "sports",
    "marca.com": "sports", "eurosport.com": "sports", "livescore.com": "sports",
    "sofascore.com": "sports", "transfermarkt.com": "sports",
    "completesports.com": "sports", "soccernet.ng": "sports",
    # entertainment
    "imdb.com": "entertainment", "netflix.com": "entertainment",
    "hulu.com": "entertainment", "rollingstone.com": "entertainment",
    "variety.com": "entertainment", "hollywoodreporter.com": "entertainment",
    "billboard.com": "entertainment", "pitchfork.com": "entertainment",
    "spotify.com": "entertainment", "tmz.com": "entertainment",
    "eonline.com": "entertainment", "ign.com": "entertainment",
    "gamespot.com": "entertainment", "polygon.com": "entertainment",
    "twitch.tv": "entertainment", "youtube.com": "entertainment",
    "notjustok.com": "entertainment",
    # news
    "bbc.com": "news", "bbc.co.uk": "news", "cnn.com": "news",
    "nytimes.com": "news", "theguardian.com": "news", "reuters.com": "news",
    "apnews.com": "news", "aljazeera.com": "news", "npr.org": "news",
    "washingtonpost.com": "news", "telegraph.co.uk": "news",
    "independent.co.uk": "news", "dw.com": "news", "france24.com": "news",
    "punchng.com": "news", "vanguardngr.com": "news", "thecable.ng": "news",
    "premiumtimesng.com": "news", "channelstv.com": "news",
    "thisdaylive.com": "news", "guardian.ng": "news", "legit.ng": "news",
    "pulse.ng": "news", "saharareporters.com": "news",
    # fashion
    "vogue.com": "fashion", "gq.com": "fashion", "elle.com": "fashion",
    "harpersbazaar.com": "fashion", "cosmopolitan.com": "fashion",
    "whowhatwear.com": "fashion", "hypebeast.com": "fashion",
    "highsnobiety.com": "fashion", "net-a-porter.com": "fashion",
    "farfetch.com": "fashion", "asos.com": "fashion", "zara.com": "fashion",
    "shein.com": "fashion", "businessoffashion.com": "fashion",
}

# Ordered most specific to most generic. 'news' is last because it turns up
# inside plenty of names that are really about something else.
KEYWORDS = (
    ("sports", ("sport", "soccer", "football", "cricket", "basketball",
                "athletic", "premierleague", "score", "fixtures")),
    ("finance", ("financ", "bank", "invest", "money", "market", "stock",
                 "crypto", "forex", "economy", "fintech", "business")),
    ("fashion", ("fashion", "style", "beauty", "cloth", "apparel", "luxury",
                 "moda", "couture")),
    ("entertainment", ("movie", "film", "music", "cinema", "celeb", "gaming",
                       "stream", "entertain", "nollywood", "hollywood")),
    ("tech", ("tech", "gadget", "software", "developer", "coding", "digital",
              "cyber", "startup", "computing")),
    ("news", ("news", "herald", "tribune", "gazette", "chronicle", "reporter",
              "times", "daily")),
)


def domain_of(page_url: str) -> Optional[str]:
    """Host of a page URL, or None when there isn't a usable one.

    'www.' is dropped because no buyer means it, and the port is not part of
    the domain. A bare 'example.com/x' is accepted as well as a full URL,
    since publishers are inconsistent about sending the scheme.
    """
    try:
        parts = urlsplit(page_url if "//" in page_url else f"//{page_url}")
        host = (parts.hostname or "").lower()
    except ValueError:
        return None
    if not host or "." not in host or " " in host:
        return None
    return host[4:] if host.startswith("www.") else host


def _path_of(page_url: str) -> str:
    try:
        return urlsplit(
            page_url if "//" in page_url else f"//{page_url}"
        ).path.lower()
    except ValueError:
        return ""


def _tokens(domain: str, path: str) -> list[str]:
    """Meaningful words in the URL, strongest signal first.

    Host labels come before path segments because the site says more than one
    article's slug does. Pure TLDs are dropped: a '.news' suffix tells you
    nothing about the page.
    """
    labels = [label for label in domain.split(".") if label not in _TLDS]
    segments = [segment for segment in path.split("/") if segment]
    words: list[str] = []
    for chunk in labels + segments:
        words.append(chunk)
        words.extend(part for part in chunk.replace("_", "-").split("-") if part)
    return words


def classify_url(page_url: str) -> tuple[Optional[str], str]:
    """Best guess at the page's category, and how it was reached.

    Returns (category, source), where source is 'section', 'known_domain',
    'keyword' or 'unknown'. A None category means the caller should fall back
    to whatever it already knows about the inventory rather than guess.
    """
    domain = domain_of(page_url)
    if not domain:
        return None, "unknown"

    # A section beats the masthead: bbc.com is a news site, but bbc.com/sport
    # is sports inventory, and that is what an advertiser is buying. Only the
    # first path segment counts, because that is where publishers put their
    # sections -- scanning the whole path would read 'techcrunch.com/2024/01/
    # money-raised' as finance.
    first_segment = next((s for s in _path_of(page_url).split("/") if s), "")
    if first_segment:
        for category, keywords in KEYWORDS:
            if any(first_segment.startswith(keyword) for keyword in keywords):
                return category, "section"

    if domain in KNOWN_DOMAINS:
        return KNOWN_DOMAINS[domain], "known_domain"

    # A registrable domain answers for its subdomains too.
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in KNOWN_DOMAINS:
            return KNOWN_DOMAINS[parent], "known_domain"

    words = _tokens(domain, _path_of(page_url))

    # Prefix matches first. Sites lead with their subject, so 'newsportal' is
    # a news portal and 'sportsdaily' is about sport -- a plain substring scan
    # gets that pair backwards, because 'newsportal' contains 'sport'.
    for category, keywords in KEYWORDS:
        for word in words:
            if any(word.startswith(keyword) for keyword in keywords):
                return category, "keyword"

    for category, keywords in KEYWORDS:
        for word in words:
            if any(keyword in word for keyword in keywords):
                return category, "keyword"

    return None, "unknown"
