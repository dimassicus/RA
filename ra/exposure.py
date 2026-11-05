"""Shared country and sector discovery for one watchlist run."""
import re
import unicodedata
from functools import lru_cache

from .aliases import contains, normalized
from .relevance import headline
from .exposure_terms import CHANNEL_TERMS, MARKET_ALIASES, REGION_MARKETS, NAMES, TRADE, FUNDING, CURRENCY, OPERATIONS, SUPPLY, PRICES
from .search import LOCALES, feed_url, fetch_feed, parse_feed

# Each phrase is both a search term and a possible credit transmission channel.
TRADE_CHANNEL = {TRADE: ("tariffs", "tariff", "export ban", "import ban", "sanctions")}
CHANNELS = {
    "country": {
        **TRADE_CHANNEL,
        FUNDING: ("interest rate", "interest rates", "rate hike", "rate cut", "sovereign default", "credit crunch"),
        CURRENCY: ("capital controls", "devaluation", "currency crisis", "inflation"),
        OPERATIONS: ("power rationing", "power cuts", "national strike"),
    },
    "sector": {
        **TRADE_CHANNEL,
        SUPPLY: ("shortage", "shortages", "power cuts", "shutdown", "supply disruption"),
        PRICES: ("price", "prices", "demand"),
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


def topic_editions(subjects):
    """Plan every sector's editions before the first linked company starts its check."""
    plan = {}
    for subject in subjects:
        links = topic_links(subject)
        locations = set()
        for branch, place in links:
            if branch != "country":
                continue
            editions = ("brazil", "mexico") if place == "latin america" else (place,)
            local = {edition for edition in editions if edition in LOCALES}
            plan.setdefault((branch, place), set()).update(local)
            locations.update(local)
        for branch, sector in links:
            if branch == "sector":
                plan.setdefault((branch, sector), set()).update(locations)
    return plan


def sector_markets(subjects):
    markets = {}
    for subject in subjects:
        links = topic_links(subject)
        places = {name for branch, name in links if branch == "country"}
        for branch, sector in links:
            if branch == "sector":
                markets.setdefault(sector, set()).update(places)
    return markets


@lru_cache(maxsize=512)
def market_names(place):
    names = [place, *MARKET_ALIASES.get(place, "").split("|"), *REGION_MARKETS.get(place, ())]
    for vocabulary in NAMES.values():
        names.extend(vocabulary.get(place, "").split("|"))
    return {normalized(n) for n in names if n}


def matching_markets(subject, article):
    # Edition, publisher and machine translations cannot establish geographic relevance.
    title = normalized(headline(article, "title"))
    places = [place for branch, place in topic_links(subject) if branch == "country"]
    return [place for place in places if any(contains(title, term) for term in market_names(place))]


def scope_sector_paths(article, subject):
    """Recheck saved sector links when reading reports from older discovery runs."""
    paths = article.get("exposure_paths") or []
    if any(p.startswith("Sector ·") for p in paths) and not matching_markets(subject, article):
        return {**article, "exposure_paths": [p for p in paths if not p.startswith("Sector ·")]}
    return article


def _native_text(value):
    # Retain accents: Vietnamese giá (price) and giả (fake) are different words.
    value = unicodedata.normalize("NFC", value.casefold()).replace("i\u0307", "i")
    return value.translate(str.maketrans({"’": "'", "‘": "'", "ʻ": "'", "ʼ": "'"}))


def search_topic(branch, topic, fetcher=fetch_feed, *, edition="", markets=None):
    names = [topic] + [alias for alias, canonical in TOPIC_NAMES[branch].items() if canonical == topic]
    channels = CHANNELS[branch]
    if edition:
        lang = LOCALES[edition][0].split("-")[0]
        # These regional feeds also index Russian-language reporting.
        languages = (lang, "ru") if lang in ("az", "uz") else (lang,)
        names = list(dict.fromkeys(name for code in languages
                                   for name in NAMES[code].get(topic, topic).split("|")))
        channels = {label: [term for code in languages for term in CHANNEL_TERMS[code][label].split("|")]
                    for label in channels}
    terms = [term for phrases in channels.values() for term in phrases]
    query = "(" + " OR ".join(f'"{name}"' for name in names) + ") (" + " OR ".join(f'"{term}"' for term in terms) + ")"
    if branch == "sector" and markets and topic in markets:
        places = sorted(markets[topic])
        if edition in places:
            places = [edition]
        geo = []
        for place in places:
            if edition:
                geo.extend(name for code in languages for name in NAMES[code].get(place, place).split("|"))
            else:
                geo.append(place)
            geo.extend(MARKET_ALIASES.get(place, "").split("|")[:1])
        geo = [name for name in geo if name]
        if geo:
            query += " (" + " OR ".join(f'"{name}"' for name in dict.fromkeys(geo)) + ")"
    url, language = feed_url(query, edition)
    hits = []
    fold = _native_text if edition else normalized
    for item in parse_feed(fetcher(url), query, language):
        title = fold(headline(item, "title"))
        def matches(term):
            phrase = fold(term)
            return phrase in title if language.startswith("th") else contains(title, phrase)
        if any(matches(name) for name in names):
            labels = [label for label, phrases in channels.items() if any(matches(term) for term in phrases)]
            if labels:
                hits.append(({**item, "source_kind": "context"}, ", ".join(labels)))
    return hits


def check_topic(branch, topic, search, editions=()):
    hits, errors = {}, []
    for edition in ("", *sorted(editions)):
        try:
            found = search(branch, topic, edition=edition)
        except Exception as exc:
            errors.append(f"{branch.title()} {topic} ({edition or 'English'}): {type(exc).__name__}: {exc}")
            continue
        for item, channel in found:
            _, labels = hits.setdefault(item["url"], (item, {}))
            labels.update(dict.fromkeys(channel.split(", ")))
    return [(item, ", ".join(labels)) for item, labels in hits.values()], " | ".join(errors)


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
            if branch == "sector":
                places = matching_markets(subject, hit)
                if not places:
                    continue
                location = " Headline market: " + ", ".join(places) + "."
            else:
                location = ""
            row = by_url.setdefault(hit["url"], dict(hit))
            path = f"{branch.title()} · {topic} ({', '.join(relations)}). Check {channel}.{location}"
            paths = row.setdefault("exposure_paths", [])
            if path not in paths:
                paths.append(path)
    return list(by_url.values())
