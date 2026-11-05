"""Shared country and sector discovery for one watchlist run."""
import re

from .aliases import contains, normalized
from .relevance import headline
from .search import feed_url, fetch_feed, parse_feed

# Each phrase is both a search term and a possible credit transmission channel.
TRADE_CHANNEL = {"import costs and access to customers": ("tariffs", "tariff", "export ban", "import ban", "sanctions")}
CHANNELS = {
    "country": {
        **TRADE_CHANNEL,
        "financing costs and access to credit": ("interest rate", "interest rates", "rate hike", "rate cut", "sovereign default", "credit crunch"),
        "foreign-currency payments and purchasing power": ("capital controls", "devaluation", "currency crisis", "inflation"),
        "continuity of operations": ("power rationing", "power cuts", "national strike"),
    },
    "sector": {
        **TRADE_CHANNEL,
        "input costs and production": ("shortage", "shortages", "power cuts", "shutdown", "supply disruption"),
        "selling prices and margins": ("price", "prices", "demand"),
    },
}
# The watchlist uses both compound industry labels and variant place names.
TOPIC_NAMES = {
    "country": {"brasil": "brazil", "turkiye": "turkey", "viet nam": "vietnam", "uk": "united kingdom"},
    "sector": {"fertilizers": "fertilizer", "fertilisers": "fertilizer", "fertiliser": "fertilizer",
               "aluminum": "aluminium", "aluminium and copper": "aluminium, copper",
               "aluminium and glass": "aluminium, glass"},
}


def topic_links(subject):
    links = {}
    for field, branch, relation in (
        ("country_registration", "country", "registered in"),
        ("primary_countries", "country", "operates in"),
        ("primary_industries", "sector", "listed industry"),
    ):
        for value in re.split(r"[,;\n]", subject.get(field) or ""):
            key = normalized(value)
            if not key:
                continue
            for topic in TOPIC_NAMES[branch].get(key, key).split(", "):
                reasons = links.setdefault((branch, topic), [])
                if relation not in reasons:
                    reasons.append(relation)
    return links


def search_topic(branch, topic, fetcher=fetch_feed):
    names = [topic] + [alias for alias, canonical in TOPIC_NAMES[branch].items() if canonical == topic]
    channels = CHANNELS[branch]
    terms = [term for phrases in channels.values() for term in phrases]
    query = "(" + " OR ".join(f'"{name}"' for name in names) + ") (" + " OR ".join(f'"{term}"' for term in terms) + ")"
    # Shared English edition keeps a global sector independent of its first company's domicile.
    url, language = feed_url(query, "")
    hits = []
    for item in parse_feed(fetcher(url), query, language):
        title = normalized(headline(item, "title"))
        if not any(contains(title, name) for name in names):
            continue
        matched = [label for label, phrases in channels.items() if any(contains(title, term) for term in phrases)]
        if matched:
            hits.append(({**item, "source_kind": "context"}, ", ".join(matched)))
    return hits


def check_topic(branch, topic, search):
    try:
        return search(branch, topic), ""
    except Exception as exc:
        return [], f"{branch.title()} {topic}: {type(exc).__name__}: {exc}"


def add_exposure_leads(subject, articles, errors, cache, check):
    by_url = {}
    for article in articles:
        by_url.setdefault(article["url"], dict(article))
    for (branch, topic), relations in topic_links(subject).items():
        key = (branch, topic)
        if key not in cache:
            cache[key] = check(branch, topic)
        hits, error = cache[key]
        if error:
            errors.append(error)
        for hit, channel in hits:
            row = by_url.setdefault(hit["url"], dict(hit))
            path = f"{branch.title()} · {topic} ({', '.join(relations)}). Check {channel}."
            paths = row.setdefault("exposure_paths", [])
            if path not in paths:
                paths.append(path)
    return list(by_url.values())
