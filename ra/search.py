"""Public news discovery. Results are leads, not verified credit findings."""

from __future__ import annotations

import re
import unicodedata
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree


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


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in value if not unicodedata.combining(character))


def signal_terms(title: str) -> list[str]:
    folded = normalized(title)
    return [label for label, words in SIGNALS.items()
            if any(normalized(word) in folded for word in words)]


def search_queries(subject: dict) -> list[str]:
    name = subject["name"].strip().replace('"', "")
    queries = [f'"{name}"']
    alias = subject.get("short_name", "").strip().replace('"', "")
    if len(alias) >= 4 and normalized(alias) != normalized(name):
        queries.append(f'"{alias}" "{subject["country_registration"]}"')
    short_name = re.sub(r"\s+(S\.?A\.?|S/A|A\.?S\.?|LTDA\.?)$", "", name, flags=re.I)
    if short_name != name and len(short_name) >= 4:
        queries.append(f'"{short_name}"')
    related = [part.strip() for part in re.split(r"[,;\n]", subject.get("affiliates", "") + ";" + subject.get("key_owners", ""))
               if part.strip()]
    if related:
        queries.append(f'"{name}" "{related[0].replace(chr(34), "")}"')
    return queries[:3]


def feed_url(query: str, country: str) -> tuple[str, str]:
    language, region, edition = LOCALES.get(normalized(country), ("en-US", "US", "US:en"))
    return "https://news.google.com/rss/search?" + urlencode(
        {"q": query, "hl": language, "gl": region, "ceid": edition}
    ), language


def fetch_feed(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "RA-Credit-Monitor/0.1 (+local research app)"})
    with urlopen(request, timeout=20) as response:
        return response.read(2_000_001)


def parse_feed(content: bytes, query: str, language: str, limit: int = 30) -> list[dict]:
    if len(content) > 2_000_000:
        raise ValueError("News feed exceeded size limit")
    root = ElementTree.fromstring(content)
    if root.tag != "rss":
        raise ValueError("Unexpected news feed format")
    articles = []
    for item in root.findall("./channel/item")[:limit]:
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        if not title or not url.startswith("https://"):
            continue
        source = item.find("source")
        date_text = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(date_text).astimezone(timezone.utc) if date_text else None
        except (TypeError, ValueError, OverflowError):
            published_at = None
        articles.append({
            "title": title,
            "url": url,
            "publisher": source.text.strip() if source is not None and source.text else "",
            "published_at": published_at,
            "query": query,
            "language": language,
            "signal_terms": signal_terms(title),
            "source_kind": "news",
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


def heringer_official_notices(fetcher=fetch_json_post, year: int | None = None) -> list[dict]:
    current_year = year or datetime.now(timezone.utc).year
    articles = []
    for filing_year in (current_year, current_year - 1):
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
            date_text = document.get("file_published_date") or document.get("file_date")
            try:
                published_at = datetime.fromisoformat(date_text.replace("Z", "+00:00")) if date_text else None
            except ValueError:
                published_at = None
            articles.append({
                "title": title, "url": url, "publisher": "Heringer RI",
                "published_at": published_at, "query": f"Official disclosures {filing_year}",
                "language": "pt-BR", "signal_terms": signal_terms(title),
                "source_kind": "official",
            })
    return articles


def search_company(subject: dict, fetcher=fetch_feed, official_fetcher=fetch_json_post) -> tuple[list[dict], list[str]]:
    articles, errors = [], []
    seen_urls = set()
    if is_heringer(subject):
        try:
            for article in heringer_official_notices(fetcher=official_fetcher):
                seen_urls.add(article["url"])
                articles.append(article)
        except Exception as exc:
            errors.append(f"Heringer RI: {type(exc).__name__}: {exc}")
    for query in search_queries(subject):
        url, language = feed_url(query, subject["country_registration"])
        try:
            for article in parse_feed(fetcher(url), query, language):
                if article["url"] not in seen_urls:
                    seen_urls.add(article["url"])
                    articles.append(article)
        except Exception as exc:
            errors.append(f"{query}: {type(exc).__name__}: {exc}")
    return articles, errors
