"""Discover public company disclosure pages; report gaps without implying full coverage."""
import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit
from xml.etree import ElementTree

from . import db
from .aliases import company_names, contains, discovery_names, normalized, provider_name
from .article_content import fetch_bytes
from .news_apis import fetch_json
from .preliminary import country_code, name_key

COVERAGE_NOTE = "Discovery is not exhaustive; exchange filings, unlinked documents and restricted pages may be missing."
IDENTITY = ("name", "short_name", "aliases", "country_registration", "registration_number", "tax_id")
TOPIC = re.compile(r"investor|disclos|financial|annual.report|press.release|news|rela[cç][oõ]es|comunicad|раскрыт|инвестор|отчет|отчёт|новост|keterbukaan", re.I)
_lock = threading.Lock()
_pending = set()
_running = False


def identity(subject):
    return hashlib.sha256(json.dumps([subject.get(k, "") for k in IDENTITY], ensure_ascii=False).encode()).hexdigest()


def wikidata(**params):
    return fetch_json("https://www.wikidata.org/w/api.php?" + urlencode(dict(format="json", **params)))


def claims(entity, key):
    return [item["mainsnak"]["datavalue"]["value"] for item in entity.get("claims", {}).get(key, [])
            if "datavalue" in item.get("mainsnak", {}) and item.get("rank") != "deprecated"]


def short_name(subject):
    return " ".join(re.sub(r"\b(?:public|private|pjsc|ojsc|jsc|pt|tbk)\b", "", name_key(subject["name"])).split())


def website_candidates(subject, lookup=wikidata):
    """Wikidata is a discovery lead, not verification of individual disclosures."""
    names = company_names(subject)
    short = short_name(subject)
    if len(short) >= 4:
        names.append(normalized(short))
    region = country_code(subject.get("country_registration", ""))
    checked, candidates = set(), []
    queries = list(dict.fromkeys([short, short + " group", provider_name(subject), *discovery_names(subject)]))[:3]
    for query in queries:
        matches = lookup(action="wbsearchentities", search=query, language="en", limit=5).get("search", [])
        for match in matches:
            key = match["id"]
            if key in checked:
                continue
            checked.add(key)
            label = normalized(match.get("label", ""))
            if not any(len(name) >= 4 and (contains(label, name) or contains(name, label)) for name in names if label):
                continue
            entity = lookup(action="wbgetentities", ids=key, props="claims")["entities"][key]
            countries = [value["id"] for value in claims(entity, "P17") if isinstance(value, dict)]
            if not countries:
                continue
            records = lookup(action="wbgetentities", ids="|".join(countries), props="claims")["entities"].values()
            if not region or not any(region in claims(record, "P297") for record in records):
                continue
            for url in claims(entity, "P856"):
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    url = "https://" + url.split("://", 1)[1]
                    if url not in [item["url"] for item in candidates]:
                        candidates.append({"url": url, "evidence": "https://www.wikidata.org/wiki/" + key})
        if candidates:
            break
    return candidates[:2]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.feeds, self.text = [], [], []
        self.anchor = None
        self.script = None
        self.structured = []
        self.hidden = False
        self.next_url = ""

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag in ("a", "link") and "next" in (fields.get("rel") or "").lower().split():
            self.next_url = fields.get("href") or ""
        if tag == "a":
            self.anchor = [fields.get("href", ""), []]
        if tag == "link" and fields.get("type") in ("application/rss+xml", "application/atom+xml"):
            self.feeds.append(fields.get("href", ""))
        if tag in ("script", "style"):
            self.hidden = True
        if tag == "script" and fields.get("type") == "application/ld+json":
            self.script = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if not self.hidden:
            self.text.append(data)
            if self.anchor is not None:
                self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            self.links.append((self.anchor[0], " ".join(self.anchor[1]).strip()))
            self.anchor = None
        if tag == "script" and self.script is not None:
            try:
                self.structured.append(json.loads("".join(self.script)))
            except ValueError:
                pass
            self.script = None
        if tag in ("script", "style"):
            self.hidden = False


def same_site(url, base):
    host, root = urlsplit(url).hostname or "", urlsplit(base).hostname or ""
    root = root.removeprefix("www.")
    return urlsplit(url).scheme == "https" and (host == root or host.endswith("." + root))


def discover(subject, lookup=wikidata, fetch=fetch_bytes):
    from . import governance_sources, search
    result = {"identity": identity(subject), "sources": [], "gaps": [], "checked_at": datetime.now(timezone.utc).isoformat()}
    direct = governance_sources.notice_source(subject)
    if direct or search.is_heringer(subject):
        result["sources"].append({"url": direct[1] if direct else "https://ri.heringer.com.br/",
                                  "label": direct[0] if direct else "Heringer investor disclosures", "kind": "adapter"})
    try:
        candidates = website_candidates(subject, lookup)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        candidates = []
        result["gaps"].append(f"Website discovery unavailable ({type(exc).__name__}).")
    for candidate in candidates:
        base = candidate["url"]
        try:
            raw, mime, final = fetch(base)
            if "html" not in mime or not same_site(final, base):
                raise ValueError("Website moved to another domain or is not HTML")
            page = Links(); page.feed(raw.decode("utf-8", errors="replace"))
            text = normalized(" ".join(page.text))
            if not any(contains(text, name) for name in company_names(subject)) and not contains(text, normalized(short_name(subject))):
                raise ValueError("Company name not confirmed on candidate website")
            pages = [(urljoin(final, href), label) for href, label in page.links
                     if TOPIC.search(label + " " + href) and same_site(urljoin(final, href), final)]
            pages.sort(key=lambda item: (
                not bool(re.search(r"disclos|раскрыт|keterbukaan|comunicad", " ".join(item), re.I)),
                not bool(re.search(r"financial|annual.report|отчет|отчёт", " ".join(item), re.I)),
                not bool(re.search(r"news|press|новост", " ".join(item), re.I))))
            seen = set()
            for url, label in pages:
                if url in seen or urlsplit(url).fragment:
                    continue
                seen.add(url)
                result["sources"].append({"url": url, "label": label[:160] or "Company disclosure page", "kind": "page", "evidence": candidate["evidence"]})
                if len(seen) >= 6:
                    break
            for path in page.feeds[:2]:
                url = urljoin(final, path)
                if same_site(url, final):
                    result["sources"].append({"url": url, "label": "Company feed", "kind": "feed", "evidence": candidate["evidence"]})
            if not seen and not page.feeds:
                result["gaps"].append("Company website found; no disclosure page or feed could be identified.")
            elif not page.feeds:
                result["gaps"].append("Disclosure pages found; dated extraction depends on their public structured data.")
        except (OSError, ValueError) as exc:
            result["sources"].append({"url": base, "label": "Candidate company website · access/identity unconfirmed",
                                      "kind": "candidate", "evidence": candidate["evidence"]})
            result["gaps"].append(f"{base}: {exc}")
    if not result["sources"]:
        result["gaps"].append("No usable company disclosure source found. General news search remains available.")
    result["status"] = "partial" if any(item["kind"] != "candidate" for item in result["sources"]) else "missing"

    return result


def structured_notices(data):
    if isinstance(data, list):
        for item in data[:200]:
            yield from structured_notices(item)
    elif isinstance(data, dict):
        if data.get("datePublished") and (data.get("headline") or data.get("name")) and data.get("url"):
            yield data
        for key in ("@graph", "itemListElement", "item"):
            if key in data:
                yield from structured_notices(data[key])


def collect(subject, *, window=None, fetch=fetch_bytes, coverage=None):
    from .search import LOCALES, parse_feed, normalized as country_name
    if not subject.get("id"):
        return [], ["Disclosure discovery requires a saved company; general news search remains available."]
    state = subject.get("disclosure_sources", {})
    coverage = coverage if coverage is not None else []
    span = dict(start=window[0].date().isoformat(), end=window[1].date().isoformat()) if window else {}
    if state.get("identity") != identity(subject):
        coverage.append(dict(source="Issuer disclosures", status="Source discovery pending", **span))
        return [], ["Disclosure sources are pending discovery or company identity has changed."]
    results, gaps = [], [gap for gap in state.get("gaps", []) if gap != COVERAGE_NOTE]
    language = LOCALES.get(country_name(subject.get("country_registration", "")), ("en",))[0]
    if not any(s["kind"] in ("feed","page") for s in state.get("sources", [])):
        coverage.append(dict(source="Issuer disclosure archive", status="No confirmed dated index", **span))

    def collect_archive(source):
        articles, problems = [], []
        url = source["url"]
        note = dict(source=source.get("label") or "Issuer disclosures", url=url, status="Unavailable", **span)
        try:
            next_url, visited, known, dates = url, set(), set(), []
            note.update(count=0, pages=0)
            while next_url:
                if next_url in visited or len(visited) >= 20:
                    note["detail"] = "Pagination ended before the archive was exhausted."
                    break
                visited.add(next_url)
                raw, mime, final = fetch(next_url)
                if not same_site(final, url):
                    raise ValueError("Source moved to another domain")
                next_url = ""
                if source["kind"] == "feed":
                    records = parse_feed(raw, url, language, limit=1000)
                else:
                    page = Links()
                    page.feed(raw.decode("utf-8", errors="replace"))
                    if page.next_url and same_site(urljoin(final, page.next_url), url):
                        next_url = urljoin(final, page.next_url)
                    records = []
                    for item in structured_notices(page.structured):
                        try:
                            date = datetime.fromisoformat(item["datePublished"].replace("Z", "+00:00"))
                        except (AttributeError, ValueError):
                            continue
                        if date.tzinfo:
                            records.append({"title": item.get("headline") or item["name"], "url": urljoin(final, item["url"]),
                                            "published_at": date, "query": final, "language": language, "signal_terms": []})
                note["pages"] += 1
                note["status"] = "Visible records only"
                for article in records:
                    day = article.get("published_at")
                    if not day or not same_site(article["url"], final) or article["url"] in known:
                        continue
                    known.add(article["url"])
                    dates.append(day)
                    if window and not window[0] <= day <= window[1]:
                        continue
                    note["count"] += 1
                    articles.append(dict(article, source_kind="news", publisher=subject.get("short_name") or subject["name"],
                                        discovery_service="Discovered company disclosures"))
                if dates:
                    note.update(first=min(dates).date().isoformat(), last=max(dates).date().isoformat())
                if window and dates and min(dates) <= window[0]:
                    break
            if not dates:
                note["detail"] = "No dated records extracted."
                problems.append(f"No dated records extracted from {url}; this does not establish that no disclosures exist.")
            elif window:
                missing = []
                if min(dates).date() > window[0].date():
                    missing.append(f"{span['start']} to {(min(dates).date() - timedelta(days=1)).isoformat()}")
                if max(dates).date() < window[1].date():
                    missing.append(f"{(max(dates).date() + timedelta(days=1)).isoformat()} to {span['end']}")
                if missing:
                    note["detail"] = (note.get("detail","") + " Coverage unconfirmed: " + ", ".join(missing)).strip()
        except (OSError, ValueError, TypeError, KeyError, ElementTree.ParseError) as exc:
            note.update(status="Incomplete", detail=str(exc)[:240])
            problems.append(f"{url}: {type(exc).__name__}: {exc}")
        return articles, note, problems

    # The existing issuer-specific collector owns adapter and candidate sources.
    archives = [source for source in state.get("sources", []) if source["kind"] not in ("adapter", "candidate")]
    # Each archive owns its pagination. Merge in discovery order to preserve duplicate precedence.
    with ThreadPoolExecutor(max_workers=2) as pool:
        for articles, note, problems in pool.map(collect_archive, archives):
            results.extend(articles)
            coverage.append(note)
            gaps.extend(problems)
    return list({item["url"]: item for item in results}.values()), gaps


def schedule(company_id=None, *, force=False):
    global _running
    companies = [db.get_company(company_id)] if company_id else db.list_companies(True)
    with _lock:
        for company in companies:
            if company and company.get("active", True) and (force or company.get("disclosure_sources", {}).get("identity") != identity(company)):
                _pending.add(company["id"])
        if _running or not _pending:
            return
        _running = True
    threading.Thread(target=_work, daemon=True, name="ra-disclosure-discovery").start()


def _work():
    global _running
    while True:
        with _lock:
            if not _pending:
                _running = False
                return
            company_id = _pending.pop()
        try:
            company = db.get_company(company_id)
            if not company or not company["active"]:
                continue
            result = discover(company)
            with db.connect() as conn:
                conn.execute("UPDATE companies SET disclosure_sources=%s WHERE id=%s AND active AND " +
                             " AND ".join(f"{key}=%s" for key in IDENTITY),
                             (db.Jsonb(result), company_id, *(company.get(key, "") for key in IDENTITY)))
        except Exception:
            logging.getLogger(__name__).exception("Disclosure discovery failed for company %s", company_id)
