"""Public news discovery. Results are leads, not verified credit findings."""

from __future__ import annotations

import re
import unicodedata
import json
import time
import pycountry
from babel import Locale
from babel.languages import get_territory_language_info
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError
from xml.etree import ElementTree

from . import keywords, news_apis, risk_factors, company_developments
from . import rss_gate
from .news_topics import excluded_query_terms, excluded_story
from .aliases import parse_aliases, provider_name, discovery_names, ambiguous, company_names, related_names, normalized as identity_normalized


LOCALES = {
    "brazil": ("pt-BR", "BR", "BR:pt"),
    "brasil": ("pt-BR", "BR", "BR:pt"),
    "portugal": ("pt-PT", "PT", "PT:pt"),
    "spain": ("es-ES", "ES", "ES:es"),
    "mexico": ("es-419", "MX", "MX:es"),
    "france": ("fr-FR", "FR", "FR:fr"),
    "indonesia": ("id-ID", "ID", "ID:id"),
    "thailand": ("th-TH", "TH", "TH:th"),
    "egypt": ("ar-EG", "EG", "EG:ar"),
    "greece": ("el-GR", "GR", "GR:el"),
    "vietnam": ("vi-VN", "VN", "VN:vi"),
    "viet nam": ("vi-VN", "VN", "VN:vi"),
    "azerbaijan": ("az-AZ", "AZ", "AZ:az"),
    "uzbekistan": ("uz-UZ", "UZ", "UZ:uz"),
    "turkey": ("tr-TR", "TR", "TR:tr"),
    "turkiye": ("tr-TR", "TR", "TR:tr"),
}

SIGNALS = {
    "debt / creditors": ("creditor", "credores", "credor", "debt", "dívida", "divida", "default", "inadimpl"),
    "restructuring": ("restructur", "recuperação judicial", "recuperacao judicial", "insolv", "bankrupt", "falência", "falencia", "mediação", "mediacao", "tutela de urgência", "tutela de urgencia"),
    "legal / regulatory": ("lawsuit", "litigation", "processo", "condenad", "multa", "sanction", "investiga", "arbitragem"),
    "operations": ("shutdown", "closure", "fechamento", "paralisa", "demiss", "layoff", "suspens"),
    "financial results": ("loss", "prejuízo", "prejuizo", "liquidity", "liquidez", "cash flow", "fluxo de caixa"),
    "management change": ("renúncia", "renuncia", "resignation", "new ceo", "novo diretor"),
}

HERINGER_IR_COMPANY_ID = "7e25b347-2f69-452f-b9e6-19237e0e6ea5"
HERINGER_IR_CATEGORY = "comunicados_fatos_relevantes_comunicados_ao_mercado_e_fatos_relevantes"
HERINGER_IR_URL = (
    "https://apicatalog.mziq.com/filemanager/company/"
    + HERINGER_IR_COMPANY_ID + "/filter/categories/year/meta"
)
FCA_WARNINGS_RSS_URL = "https://www.fca.org.uk/news/warnings/rss.xml"
UK_NAMES = {"united kingdom", "uk", "great britain", "england", "scotland", "wales", "northern ireland"}


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.strip().casefold())
    return "".join(character for character in value if not unicodedata.combining(character))


def _country_locales():
    """Extend established editions with each country's main official language."""
    names = Locale("en").territories
    for country in pycountry.countries:
        region = country.alpha_2
        info = get_territory_language_info(region)
        if not info:
            continue
        code = max(info, key=lambda code: (
            info[code].get("official_status") in ("official", "de_facto_official"),
            info[code].get("population_percent") or 0,
        )).replace("_", "-")
        language = code.split("-")[0]
        if language == "zh" and "-" not in code:
            code = "zh-Hans"
        locale = LOCALES.get(normalized(country.name), (f"{language}-{region}", region, f"{region}:{code}"))
        for name in (country.name, country.alpha_2, country.alpha_3,
                     getattr(country, "official_name", ""), getattr(country, "common_name", ""), names.get(region, "")):
            if name:
                LOCALES.setdefault(normalized(name), locale)


_country_locales()


def signal_terms(title: str, language: str = "") -> list[str]:
    from .relevance import risk_signals
    return risk_signals(title, language)


def search_queries(subject: dict) -> list[str]:
    name = subject["name"].strip().replace('"', "")
    queries = [f'"{name}"']
    for alias in discovery_names(subject)[1:]:
        context = subject.get("country_registration", "").replace('"', "")
        if ambiguous(alias):
            if context:
                queries.append(f'"{alias}" "{context}"')
        else:
            queries.append(f'"{alias}"')
    short_name = re.sub(r"\s+(S\.?A\.?|S/A|A\.?S\.?|LTDA\.?)$", "", name, flags=re.I)
    if short_name != name and len(short_name) >= 4 and f'"{short_name}"' not in queries:
        queries.append(f'"{short_name}"')
    related = [part.strip() for part in re.split(r"[,;\n]", subject.get("affiliates", "") + ";" + subject.get("key_owners", ""))
               if part.strip()]
    if related:
        queries.append(f'"{name}" "{related[0].replace(chr(34), "")}"')
    return queries[:3]


def governance_searches(subject):
    """Short queries per language avoid the feed silently dropping long OR lists."""
    country = subject.get("country_registration", "")
    lang = LOCALES.get(normalized(country), ("en",))[0].split("-")[0]
    editions = [("en", "")]
    if lang != "en":
        editions.append((lang, country))
    if lang != "ru":
        editions.append(("ru", "russia"))
    if lang in ("az", "uz"):
        editions.append(("ru", country))
    names = [provider_name(subject), *parse_aliases(subject.get("aliases", ""))]
    plans = []
    local_anchor = names[0]
    factors = risk_factors.active("company")
    for code, edition in editions:
        # Prefer a reviewed local-script alias when the register uses another alphabet.
        script = {"el": r"[Α-ω]", "ar": r"[ء-ي]", "ru": r"[А-я]"}.get(code)
        anchor = next((n for n in names if script and re.search(script, n)), names[0])
        if code == lang:
            local_anchor = anchor
        for factor in factors:
            if factor["english"] in company_developments.TERMS:
                terms = company_developments.cues(factor, code)
                # Shorter issuer names cover headlines that omit the legal prefix or suffix.
                issuer = next((n for n in (subject.get("short_name"), anchor) if n and not ambiguous(n)), anchor)
                for expression in query_groups([f'"{term}"' for term in terms], limit=4):
                    plans.append((f'"{issuer}" {expression}', edition))
                if terms:
                    continue
            groups = risk_factors.GOVERNANCE_GROUPS.get(factor["english"])
            if groups is None:
                phrase = risk_factors.search_phrase(factor, code)
                if phrase:
                    plans.append((f'"{anchor}" {phrase}', edition))
                continue
            terms = keywords.discovery_terms(groups, code)
            if code == "en" and "chief executive officer" in terms:
                terms.append("CEO")
            if code == "en" and "chief financial officer" in terms:
                terms.append("CFO")
            if terms:
                expression = " OR ".join(f'"{term}"' for term in terms)
                plans.append((f'"{anchor}" ({expression})', edition))
    # Official notices often name the issuer without using a governance keyword.
    region = LOCALES.get(normalized(country), ("", ""))[1]
    domains = {
        "ID": ("idx.co.id", "ksei.co.id"), "AZ": ("vergiler.az", "taxes.gov.az"), "BR": ("gov.br/cvm", "b3.com.br"),
        "EG": ("egx.com.eg", "fra.gov.eg"), "GR": ("businessportal.gr", "athexgroup.gr", "athens.euronext.com"),
        "VN": ("hsx.vn", "hnx.vn"), "UZ": ("openinfo.uz", "uzse.uz"), "TR": ("kap.org.tr", "ticaretsicil.gov.tr"),
    }
    if region == "KZ":
        for names in query_groups([f'"{name}"' for name in discovery_names(subject, limit=None)], limit=4):
            plans.append((f"{names} site:gov.kz/org/knb", country))
    if region in domains:
        expr = " OR ".join("site:" + site for site in domains[region])
        plans.append((f'"{local_anchor}" ({expr})', country))
    # Keep name searches independent of vocabulary, including executives leaving for another issuer.
    enabled = {factor["english"] for factor in factors}
    for field, factor in (("top_management", risk_factors.MANAGEMENT), ("key_owners", risk_factors.OWNERSHIP)):
        if factor not in enabled:
            continue
        names = [re.sub(r"\s*\([^)]*\)", "", part).strip().replace('"', '') for part in
                 re.split(r"[,;\n]", subject.get(field) or "")]
        persons = list(dict.fromkeys(n[:100] for n in names if len(n) >= 4))
        for start in range(0, len(persons), 4):
            expression = " OR ".join(f'"{n}"' for n in persons[start:start+4])
            plans.append((f'"{provider_name(subject)}" ({expression})', country))
    return list(dict.fromkeys(plans))


def query_groups(expressions, limit=8, max_chars=320):
    """Bound each OR expression without dropping later terms or names."""
    batch = []
    for expression in dict.fromkeys(expressions):
        if batch and (len(batch) >= limit or len(" OR ".join([*batch, expression])) > max_chars):
            yield "(" + " OR ".join(batch) + ")"
            batch = []
        batch.append(expression)
    if batch:
        yield "(" + " OR ".join(batch) + ")"


def adverse_searches(subject):
    """Company/related-person adverse searches, independent of governance-change switches."""
    from .exposure import topic_editions
    from .model_store import country_languages, EXCLUDED_LANGUAGES
    country = subject.get("country_registration", "")
    lang = LOCALES.get(normalized(country), ("en",))[0].split("-")[0]
    editions = [("en", "")]
    if lang != "ru":
        editions.append(("ru", "russia"))
    codes = set(country_languages(country)) | {lang}
    if lang in ("az", "uz"):
        codes.add("ru")
    editions.extend((code, country) for code in sorted(codes - {"en"} - EXCLUDED_LANGUAGES))
    places = {place for locations in topic_editions([subject]).values() for place in locations}
    for place in sorted(places):
        code = LOCALES[place][0].split("-")[0]
        if code != "en" and code not in EXCLUDED_LANGUAGES and LOCALES[place] != LOCALES.get(normalized(country)):
            editions.append((code, place))
    quote = lambda text: '"' + text.replace('"', '').strip() + '"'
    companies = [quote(name) + (" " + quote(country) if ambiguous(name) and country else "")
                 for name in discovery_names(subject, limit=None) if not ambiguous(name) or country]
    people = [quote(name) for name in related_names(subject)]
    anchors = list(query_groups([*companies, *people], limit=6))
    plans = []
    for code, edition in dict.fromkeys(editions):
        terms = keywords.adverse_search_terms(code)
        # A quoted shorter phrase already retrieves its longer literal expansions.
        # Keep every phrase in the catalog, while avoiding redundant feed requests.
        terms = [term for term in terms if not any(
            other.casefold() in term.casefold() and re.search(
                r"(?<!\w)" + re.escape(other.casefold()) + r"(?!\w)", term.casefold())
            for other in terms if len(other) < len(term))]
        for expression in query_groups([quote(term) for term in terms]):
            plans.extend((f"{anchor} {expression}", edition) for anchor in anchors)
        # Title queries find unnamed executives/owners, always with company context.
        for roles in query_groups([quote(term) for term in keywords.person_title_terms(code)]):
            plans.extend((f"{anchor} {roles}", edition) for anchor in query_groups(companies, limit=6))
    return list(dict.fromkeys(plans))


def company_search_plan(subject):
    country = subject.get("country_registration", "")
    editions = [country]
    if LOCALES.get(normalized(country), ("en",))[0].split("-")[0] != "ru":
        editions.append("russia")
    if LOCALES.get(normalized(country), ("en",))[0].split("-")[0] != "en":
        editions.append("")
    return list(dict.fromkeys([(q, edition) for q in search_queries(subject) for edition in editions]
                             + governance_searches(subject) + adverse_searches(subject)))


def feed_url(query: str, country: str, *, window=None) -> tuple[str, str]:
    language, region, edition = LOCALES.get(normalized(country), ("en-US", "US", "US:en"))
    exclusions = " ".join(f'-"{term}"' for term in excluded_query_terms(language))
    dates = "when:7d"
    if window:
        # Pad date-only provider bounds; the worker enforces the exact publication instants.
        start = window[0].astimezone(timezone.utc).date() - timedelta(days=1)
        end = window[1].astimezone(timezone.utc).date() + timedelta(days=1)
        dates = f"after:{start.isoformat()} before:{end.isoformat()}"
    return "https://news.google.com/rss/search?" + urlencode(
        {"q": f"{query} {exclusions} {dates}", "hl": language, "gl": region, "ceid": edition}
    ), language


def fetch_feed(url: str, *, probe=None) -> bytes:
    if urlparse(url).hostname == "news.google.com" and urlparse(url).path == "/rss/search":
        return rss_gate.google.read(_fetch_feed, url, probe or feed_url("news", "")[0])
    return _fetch_feed(url)


def _fetch_feed(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "RA-Credit-Monitor/0.1 (+local research app)"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                return response.read(2_000_001)
        except (TimeoutError, URLError) as exc:
            reason = exc.reason if isinstance(exc, URLError) else exc
            if not isinstance(reason, TimeoutError) or attempt == 2:
                raise
            time.sleep(2 ** attempt)


def parse_feed(content: bytes, query: str, language: str, limit: int = 30, *, window=None) -> list[dict]:
    if len(content) > 2_000_000:
        raise ValueError("News feed exceeded size limit")
    root = ElementTree.fromstring(content)
    if root.tag != "rss":
        raise ValueError("Unexpected news feed format")
    articles = []
    for item in root.findall("./channel/item"):
        if len(articles) >= limit:
            break
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if not title or not url.startswith("https://"):
            continue
        source = item.find("source")
        publisher = source.text.strip() if source is not None and source.text else ""
        if excluded_story(title, url, publisher=publisher, categories=[c.text for c in item.findall("category")]):
            continue
        date_text = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(date_text).astimezone(timezone.utc) if date_text else None
        except (TypeError, ValueError, OverflowError):
            published_at = None
        if window and (not published_at or not window[0] <= published_at <= window[1]):
            continue
        description = item.findtext("description") or ""
        articles.append({
            "summary": description[:1500] if "<" not in description else "",
            "title": title,
            "url": url,
            "publisher": publisher,
            "published_at": published_at,
            "query": query,
            "language": language,
            "signal_terms": signal_terms(title, language),
            "source_kind": "news",
            "discovery_service": "Google News RSS",
        })
    return articles


def fetch_json_post(url: str, payload: dict) -> dict:
    request = Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "RA-Credit-Monitor/0.1 (+local research app)"},
    )
    with urlopen(request, timeout=20) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("Investor-relations response exceeded size limit")
    return json.loads(raw)


def is_heringer(subject: dict) -> bool:
    if normalized(subject.get("country_registration", "")) not in ("brazil", "brasil"):
        return False
    registration = re.sub(r"\D", "", subject.get("tax_id", "") or subject.get("registration_number", ""))
    return "22266175000188" in registration or normalized(subject["name"]).strip() in (
        "fertilizantes heringer", "fertilizantes heringer s.a.", "fertilizantes heringer s/a",
    ) or normalized(subject["name"]).startswith("fertilizantes heringer sociedade")


def has_uk_presence(subject: dict) -> bool:
    countries = re.split(r"[,;\n]", subject.get("primary_countries", ""))
    return any(normalized(value.strip()) in UK_NAMES for value in
               [subject.get("country_registration", ""), *countries])


def fca_warning_list(subject: dict, fetcher=fetch_feed) -> list[dict]:
    """Find possible name matches in the FCA's current warning RSS; analyst review is required."""
    names = company_names(subject)
    if not names:
        return []
    warnings = parse_feed(fetcher(FCA_WARNINGS_RSS_URL), "FCA Warning List", "en-GB", limit=1000)
    matches = []
    for warning in warnings:
        if urlparse(warning["url"]).hostname not in {"fca.org.uk", "www.fca.org.uk"}:
            continue
        title = identity_normalized(warning["title"])
        if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", title) for name in names):
            warning["publisher"] = "Financial Conduct Authority"
            warning["source_kind"] = "official"
            warning["discovery_service"] = "FCA Warning RSS"
            matches.append(warning)
    return matches


def heringer_official_notices(fetcher=fetch_json_post, year: int | None = None, *, start_year: int | None = None) -> list[dict]:
    current_year = year or datetime.now(timezone.utc).year
    articles = []
    for filing_year in range(current_year, (start_year if start_year is not None else current_year - 1) - 1, -1):
        response = fetcher(HERINGER_IR_URL, {
            "year": filing_year, "categories": [HERINGER_IR_CATEGORY],
            "language": "pt_BR", "published": True,
        })
        if not response.get("success"):
            raise ValueError("Investor-relations feed reported failure")
        for document in response.get("data", {}).get("document_metas", [])[:100]:
            title = (document.get("file_title") or "").strip()
            url = document.get("link_url") or document.get("permalink") or ""
            if not title or not url.startswith("https://"):
                continue
            if excluded_story(title, url):
                continue
            date_text = document.get("file_published_date") or document.get("file_date")
            try:
                published_at = datetime.fromisoformat(date_text.replace("Z", "+00:00")) if date_text else None
            except ValueError:
                published_at = None
            articles.append({
                "title": title, "url": url, "publisher": "Heringer RI",
                "published_at": published_at, "query": f"Official disclosures {filing_year}",
                "language": "pt-BR", "signal_terms": signal_terms(title, "pt"),
                "source_kind": "official",
                "discovery_service": "Heringer RI",
            })
    return articles


def planned_feeds(subject, window, adverse_focus=False):
    # Split broad name searches: one capped RSS feed cannot cover months of news.
    name_queries = set(search_queries(subject))
    windows = [window]
    if window and window[1] - window[0] > timedelta(days=28):
        start, end = window
        step = max(timedelta(days=28), (end - start) / 24)
        windows = []
        while start < end:
            stop = min(start + step, end)
            windows.append((start, stop))
            start = stop
    plan = company_search_plan(subject)
    if adverse_focus:
        filing_query = search_queries(subject)[0] + ' ("exchange inquiry" OR "unusual trading" OR "material disclosure" OR "audit committee change")'
        plan = [(filing_query, subject.get("country_registration", "")), *plan]
        plan.sort(key=lambda item: item[0] in name_queries)
    return [(query, edition, bounds) for query, edition in plan
               for bounds in (windows if query in name_queries else [window])]


def search_company(subject, fetcher=fetch_feed, official_fetcher=fetch_json_post, **options):
    steps = search_company_steps(subject, fetcher, official_fetcher, **options)
    while True:
        try:
            next(steps)
        except StopIteration as finished:
            return finished.value


def search_company_steps(subject, fetcher=fetch_feed, official_fetcher=fetch_json_post, *, progress=None,
                         window=None, on_deferred=None, provider_window=None, filing_coverage=None, settings=None):
    articles, errors = [], []
    seen_urls = set()
    from . import disclosures, idx_filings
    coverage = filing_coverage if filing_coverage is not None else []
    span = provider_window or window
    discovered, gaps = disclosures.collect(subject, window=span, coverage=coverage)
    yield
    filings, filing_gaps = idx_filings.collect(subject, fetcher, span, coverage=coverage)
    gaps = [*gaps, *filing_gaps]
    for article in [*discovered, *filings]:
        if article["url"] not in seen_urls:
            articles.append(article)
            seen_urls.add(article["url"])
    errors.extend("Disclosure coverage: " + gap for gap in gaps)
    from .governance_sources import notice_source, recent_notices
    source = notice_source(subject)
    if source:
        yield
        notices = recent_notices(subject, *source, fetcher, errors=errors, window=window)
        if span:
            failures = [e for e in errors if e.startswith(source[0] + ":")]
            coverage.append(dict(source=source[0], url=source[1], start=span[0].date().isoformat(),
                                 end=span[1].date().isoformat(), count=len(notices),
                                 status="Incomplete" if failures else "Dated notices checked", detail=" ".join(failures)))
        for article in notices:
            if article["url"] not in seen_urls:
                seen_urls.add(article["url"])
                articles.append(article)
    if is_heringer(subject):
        try:
            for article in heringer_official_notices(fetcher=official_fetcher, year=window[1].year if window else None,
                                                     start_year=window[0].year if window else None):
                seen_urls.add(article["url"])
                articles.append(article)
        except Exception as exc:
            errors.append(f"Heringer RI: {type(exc).__name__}: {exc}")
    if has_uk_presence(subject):
        try:
            for article in fca_warning_list(subject, fetcher=fetcher):
                seen_urls.add(article["url"])
                articles.append(article)
        except Exception as exc:
            errors.append(f"FCA Warning List: {type(exc).__name__}: {exc}")
    configured = news_apis.configured_providers()
    selected = subject.get("_news_providers") if "_news_providers" in subject else configured
    if configured and not selected:
        errors.append("News APIs: daily request budget allocation unavailable for this company")
    for provider in selected:
        if provider not in configured:
            continue
        yield
        try:
            provider_articles = news_apis.search(provider, subject, window=window)
        except news_apis.ProviderError as exc:
            provider_articles = exc.articles
            errors.append(f"{provider}: {exc}")
        except Exception as exc:
            provider_articles = []
            errors.append(f"{provider}: {type(exc).__name__}: request could not be completed")
        for article in provider_articles:
            if article["url"] not in seen_urls:
                seen_urls.add(article["url"])
                articles.append(article)
    from . import google_discovery, perplexity, search_settings
    settings = settings or search_settings.for_run({})
    adverse_focus = settings.get("adverse_only", False)
    queries = planned_feeds(subject, window, adverse_focus)
    for index, (query, edition, bounds) in enumerate(queries):
        yield
        if progress:
            progress(f"Searching company news · query {index + 1} of {len(queries)}", index, len(queries))
        url, language = feed_url(query, edition, window=bounds)
        try:
            probe = feed_url('"' + provider_name(subject) + '"', edition, window=bounds)[0]
            raw = fetcher(url, probe=probe) if fetcher is fetch_feed else fetcher(url)
            for article in parse_feed(raw, query, language, limit=100, window=bounds):
                if article["url"] not in seen_urls:
                    seen_urls.add(article["url"])
                    articles.append(article)
        except rss_gate.Deferred as exc:
            if on_deferred:
                on_deferred(query, edition, bounds)
            elif str(exc) not in errors:
                errors.append(str(exc))
        except Exception as exc:
            errors.append(f"{query}: {type(exc).__name__}: {exc}")
    from .article_content import canonical_url
    yield
    extra, gaps = (perplexity.collect(subject, span, articles, progress, adverse_focus=adverse_focus)
                   if settings["use_perplexity"] else ([], []))
    if settings["use_google"]:
        yield
        google_hits, google_gaps = google_discovery.collect(subject, span, [*articles, *extra], coverage,
            primary=not settings["use_perplexity"] or not perplexity.recovery_providers.configured("Perplexity"),
            provider_gaps=gaps, progress=progress, adverse_focus=adverse_focus)
        extra.extend(google_hits)
        gaps.extend(google_gaps)
    known = {canonical_url(a['url']): a for a in articles}
    for hit in extra:
        original = known.get(hit['url'])
        if original is None:
            articles.append(hit)
            known[hit['url']] = hit
        elif hit.get('decision_followup_urls'):
            original['decision_followup_urls'] = list(dict.fromkeys([
                *original.get('decision_followup_urls', []), *hit['decision_followup_urls']]))
    errors.extend(gaps)
    return articles, errors
