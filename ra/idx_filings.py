"""Read issuer-matched IDX document links from JejakSaham's public announcement index."""
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from .aliases import headline_identity, normalized, parse_aliases
from .governance_sources import VisibleText
from .preliminary import country_code

ZONE = ZoneInfo("Asia/Jakarta")
MONTHS = "jan feb mar apr mei jun jul agu sep okt nov des".split()
LINK = re.compile(r'https://www\.idx\.co\.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/[\w%./-]+\.pdf', re.I)
ARCHIVE_SERVICE = "Issuer disclosures via Brave Search"


def text(markup):
    parser = VisibleText()
    parser.feed(markup)
    return " ".join(parser.lines)


def historical_asset_notices(subject, start, end):
    """Find indexed issuer notices in the part of a historical window missing from IDX."""
    from . import disclosures, recovery_providers
    state = subject.get("disclosure_sources") or {}
    if state.get("identity") != disclosures.identity(subject) or not recovery_providers.configured("Brave"):
        return []
    site = next((s["url"] for s in state.get("sources", []) if s.get("kind") in ("candidate", "page", "feed")
                 and s.get("evidence", "").startswith("https://www.wikidata.org/wiki/")), "")
    host = (urlsplit(site).hostname or "").removeprefix("www.")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]+", host):
        return []
    query = f'site:{host} ("assets" OR "aset" OR "court" OR "pengadilan")'
    bounds = f"{start.astimezone(ZONE):%Y-%m-%d}to{end.astimezone(ZONE):%Y-%m-%d}"
    params = urlencode(dict(q=query, freshness=bounds, count=20, country="ALL", spellcheck="false"))
    payload = recovery_providers.request("Brave", "/res/v1/web/search?" + params)
    rows = []
    for hit in (payload.get("web") or {}).get("results", []):
        url = hit.get("url", "")
        title = text(hit.get("title", ""))
        if not disclosures.same_site(url, site) or not headline_identity(normalized(title), subject):
            continue
        try:
            stamp = datetime.fromisoformat((hit.get("page_age") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        # The index may omit the timezone. Use its day, not an inferred publication time.
        if stamp.tzinfo is None:
            stamp = datetime.combine(stamp.date(), datetime.min.time(), ZONE)
            if stamp < start:
                continue
            stamp += timedelta(days=1, microseconds=-1)
        if start <= stamp <= end:
            rows.append(dict(title=title, summary=text(hit.get("description") or "")[:1500], url=url,
                             published_at=stamp, query=query, publisher=host, discovery_service=ARCHIVE_SERVICE,
                             language=hit.get("language") or "", source_kind="news", signal_terms=[]))
    return rows


def collect(subject, fetch, window=None):
    legal = normalized(subject["name"])
    if country_code(subject.get("country_registration", "")) != "ID" or not re.fullmatch("pt .+ tbk", legal):
        return [], []
    symbols = [alias for alias in parse_aliases(subject.get("aliases", "")) if re.fullmatch("[A-Z]{4}", alias)]
    if len(symbols) != 1:
        return [], ["IDX filing index requires one reviewed stock ticker for this issuer."]
    ticker = symbols[0]
    url = f"https://jejaksaham.id/saham/{ticker}?tab=profil"
    records, gaps = [], []
    now = datetime.now(timezone.utc)
    start, end = window or (now - timedelta(days=7), now)
    try:
        raw = fetch(url)
        if len(raw) > 2_000_000:
            raise ValueError("filing index exceeded size limit")
        page = raw.decode("utf-8")
        heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", page, re.S)
        if not heading or normalized(text(heading[1])) != ticker.lower() + " " + legal[3:] + " pt":
            raise ValueError("ticker and full issuer name did not match the index")
        section = re.search(r'<section id="pengumuman"[^>]*>(.*?)</section>', page, re.S)
        if not section:
            raise ValueError("announcement section unavailable")
        dates = []
        for row in re.findall(r"<li\b[^>]*>(.*?)</li>", section[1], re.S):
            date = re.search(r"<span\b[^>]*>(\d{1,2}) (\w+) (\d{4})</span>", row)
            link = re.search(r'<a href="([^"]+)"[^>]*>(.*?)</a>', row, re.S)
            if not date or not link:
                raise ValueError("unrecognised announcement row")
            day = datetime(int(date[3]), MONTHS.index(date[2].lower()) + 1, int(date[1]), tzinfo=ZONE)
            dates.append(day)
            href = unescape(link[1])
            if not LINK.fullmatch(href) or not start <= day or day + timedelta(days=1, microseconds=-1) > end:
                continue
            records.append(dict(title=subject["name"] + " — " + text(link[2]), url=href, published_at=day,
                                query=url, publisher="IDX", discovery_service="IDX filings via JejakSaham",
                                language="id", source_kind="news", signal_terms=[]))
        if not dates:
            gaps.append("The IDX filing index listed no dated announcements. Coverage of the selected window is unknown.")
        elif min(dates).date() > start.astimezone(ZONE).date():
            gaps.append(f"The earliest announcement listed in the IDX filing index is dated {min(dates):%Y-%m-%d}. "
                        f"The requested window starts on {start.astimezone(ZONE):%Y-%m-%d}; earlier filings may be missing.")
            if window:
                try:
                    records.extend(historical_asset_notices(subject, start, min(end, min(dates) - timedelta(microseconds=1))))
                except (ValueError, OSError) as exc:
                    gaps.append(f"Historical issuer disclosure search: {exc}")
    except (ValueError, OSError) as exc:
        gaps.append(f"IDX filing index: {exc}")
    return records, gaps
