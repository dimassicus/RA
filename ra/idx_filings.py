"""Read issuer-matched IDX document links from JejakSaham's public announcement index."""
import json
import re
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import quote, urlencode, urlsplit
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


IDX = "https://block.idx.id/primary/ListedCompany/GetAnnouncement"


def exchange_archive(subject, ticker, start, end, fetch, coverage):
    """Count every index row before deduplicating repeated submissions."""
    offset, periods, rows, seen = 0, set(), [], set()
    note = dict(source="IDX issuer announcements", url=IDX, count=0, status="Incomplete",
                start=start.astimezone(ZONE).date().isoformat(), end=end.astimezone(ZONE).date().isoformat())
    coverage.append(note)
    try:
        profile, = json.loads(fetch(IDX.replace("GetAnnouncement", "GetCompanyProfilesDetail") + "?KodeEmiten=" + ticker))["Profiles"]
        if profile["KodeEmiten"].strip() != ticker or normalized(profile["NamaEmiten"]) != normalized(subject["name"]):
            raise ValueError("IDX profile identity mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        note["detail"] = str(exc)[:240]
        return rows, False
    letters = set()
    total = None
    while total is None or offset < total:
        query = urlencode(dict(lang="id", kodeEmiten=ticker, emitenType="s", indexFrom=offset, pageSize=100,
                               dateFrom=note["start"].replace("-", ""), dateTo=note["end"].replace("-", "")))
        url = IDX + "?" + query
        try:
            raw = fetch(url)
            if len(raw) > 2_000_000:
                raise ValueError("IDX index page exceeded size limit")
            data = json.loads(raw)
            note["url"] = url
            params = data["SearchParams"]
            if (datetime.strptime(params["DateFrom"], "%m/%d/%Y %I:%M:%S %p").date().isoformat() != note["start"]
                    or datetime.strptime(params["DateTo"], "%m/%d/%Y %I:%M:%S %p").date().isoformat() != note["end"]
                    or params["KodeEmiten"] != ticker or int(params["indexfrom"]) != offset
                    or type(data["ResultCount"]) is not int or data["ResultCount"] < 0
                    or (total is not None and total != data["ResultCount"])):
                raise ValueError("IDX query or count changed")
            total = data["ResultCount"]
            page = data["Replies"]
            if not isinstance(page, list) or (not page and offset < total) or offset + len(page) > total:
                raise ValueError("Incomplete IDX pagination")
            for item in page:
                entry = item["pengumuman"]
                if entry["Kode_Emiten"].strip() != ticker or entry["JenisPengumuman"] != "STOCK":
                    raise ValueError("IDX issuer or market mismatch")
                if entry["Id2"] in seen:
                    raise ValueError("Repeated IDX page or record")
                seen.add(entry["Id2"])
                day = datetime.fromisoformat(entry["TglPengumuman"]).replace(tzinfo=ZONE)
                if not start <= day <= end:
                    continue
                files = []
                for attachment in item["attachments"]:
                    path = quote(attachment["FullSavePath"], safe=":/%.-_")
                    if LINK.fullmatch(path):
                        files.append((attachment["OriginalFilename"], path.replace("://www.idx.co.id/", "://block.idx.id/", 1)))
                if not files:
                    note["without_pdf"] = note.get("without_pdf", 0) + 1
                    continue
                # The exchange-standard statement is the comparative table; the other PDFs are enclosures.
                files.sort(key=lambda f: not f[0].lower().startswith("financialstatement-"))
                filename, pdf = files[0]
                period = re.fullmatch(r"FinancialStatement-(\d{4})-(I{1,3}|IV|Tahunan)-" + ticker + r"\.pdf", filename, re.I)
                label = ""
                if period:
                    label = {"I":"Q1","II":"H1","III":"9M","IV":"Q4","TAHUNAN":"FY"}[period[2].upper()] + " " + period[1]
                    periods.add(label)
                letter = (entry["NoPengumuman"], entry["JudulPengumuman"], day.date(), tuple(name for name,_ in files))
                if letter in letters:
                    continue
                letters.add(letter)
                rows.append(dict(title=subject["name"] + " — " + entry["JudulPengumuman"] + (" · " + label if label else ""),
                                 url=pdf, published_at=day, query=url, publisher="IDX", discovery_service="IDX issuer announcements",
                                 filing_attachments=[link for _, link in files[1:4]], language="id", source_kind="news", signal_terms=[]))
            offset += len(page)
            note.update(count=len(rows), periods=sorted(periods, key=lambda p: (p.split()[1], ("Q1","H1","9M","Q4","FY").index(p.split()[0]))),
                        entries_read=offset)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            note["detail"] = str(exc)[:240]
            return rows, False
    note["status"] = "Index checked"
    return rows, True


def collect(subject, fetch, window=None, *, coverage=None):
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
    if coverage is not None:
        records, complete = exchange_archive(subject, ticker, start, end, fetch, coverage)
        if complete:
            return records, []
        gaps.append("IDX dated archive incomplete. " + coverage[-1]["detail"])
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
        if coverage is not None:
            note = dict(source="IDX via JejakSaham", url=url, status="Visible records only",
                        start=start.date().isoformat(), end=end.date().isoformat(), count=sum(r["query"] == url for r in records))
            if dates:
                note.update(first=min(dates).date().isoformat(), last=max(dates).date().isoformat())
            coverage.append(note)
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
