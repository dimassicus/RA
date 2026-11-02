"""Cheap, source-linked legal-identity precheck for intake drafts."""

from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import db, search


COUNTRY_CODES = {
    "brazil": "BR", "brasil": "BR", "egypt": "EG", "greece": "GR",
    "vietnam": "VN", "viet nam": "VN", "azerbaijan": "AZ",
    "uzbekistan": "UZ", "turkey": "TR", "türkiye": "TR",
}


def country_code(value: str) -> str:
    try:
        import pycountry
        return pycountry.countries.lookup(value).alpha_2
    except (LookupError, ImportError):
        return COUNTRY_CODES.get(value.strip().casefold(), "")


def folded(value: str) -> str:
    text = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


LEGAL_WORDS = {"sa", "s", "a", "as", "sociedade", "anonima", "joint", "stock", "company",
               "corporation", "corp", "limited", "ltd", "llc", "the", "societe", "anonyme"}


def name_key(value: str) -> str:
    return " ".join(word for word in re.findall(r"[^\W_]+", folded(value), flags=re.UNICODE)
                    if word not in LEGAL_WORDS)


def name_score(expected: str, found: str) -> float:
    a, b = name_key(expected), name_key(found)
    if not a or not b:
        return 0.0
    if a == b or (min(len(a), len(b)) >= 12 and (a.startswith(b) or b.startswith(a))):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def id_equal(left: str, right: str) -> bool:
    a = "".join(c for c in unicodedata.normalize("NFKC", left).upper() if c.isalnum())
    b = "".join(c for c in unicodedata.normalize("NFKC", right).upper() if c.isalnum())
    return bool(a and b and (a == b or (a.isdigit() and b.isdigit() and a.lstrip("0") == b.lstrip("0"))))


def fetch_json(url: str) -> dict:
    request = Request(url, headers={"User-Agent": "RA-Credit-Monitor/0.2 (analyst identity review)",
                                    "Accept": "application/json"})
    with urlopen(request, timeout=12) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("Registry response exceeded size limit")
    return json.loads(raw)


def brazil_cnpjs(value: str) -> list[str]:
    return list(dict.fromkeys(re.sub(r"\D", "", item) for item in
                              re.findall(r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|(?<!\d)\d{14}(?!\d)", value)))


def address_text(address: dict) -> str:
    return ", ".join(str(part) for part in [*(address.get("addressLines") or []),
                                            address.get("city"), address.get("region"),
                                            address.get("postalCode"), address.get("country")] if part)


def research_company(data: dict, json_fetcher=fetch_json, feed_fetcher=search.fetch_feed) -> tuple[dict, dict, str]:
    """Suggest only fields from name+identifier+country matches; keep weak leads separate."""
    proposed = dict(data)
    findings = {"matches": [], "identity_evidence": [], "news": [], "errors": [], "filled": {},
                "conflicts": [], "possible_duplicates": []}
    country = country_code(data["country_registration"])
    ids = db.identifier_tokens(data.get("registration_number", ""), data["country_registration"])
    names = [name for name in (data["name"], data.get("short_name", "")) if name]
    cnpjs = brazil_cnpjs(data.get("registration_number", ""))[:2] if country == "BR" else []

    def add_match(source: str, url: str, legal_name: str, identifier: str, source_country: str,
                  extra_names=(), suggestions=None):
        score = max((name_score(name, candidate) for name in names
                     for candidate in [legal_name, *extra_names]), default=0)
        same_country = bool(country and source_country == country)
        same_id = any(id_equal(item, identifier) for item in ids)
        verified = same_country and same_id and score >= 0.68
        verdict = "Name, country and identifier match" if verified else (
            "Identifier and country match; name differs—check spelling or entity" if same_country and same_id else
            "Identifier mismatch" if same_country and score >= 0.68 and ids and identifier else
            "Possible name match only; check legal entity")
        findings["matches"].append({"source_name": source, "source_url": url, "legal_name": legal_name,
                                    "identifier": identifier, "country": source_country, "verdict": verdict,
                                    "name_score": round(score, 2), "verified": verified})
        if verified:
            normalized_id = "".join(c for c in identifier.upper() if c.isalnum())
            findings["identity_evidence"].append({"source_name": source, "source_url": url,
                                                    "identifier_type": "CNPJ" if source_country == "BR" else "registration",
                                                    "identifier_value": normalized_id, "verified": True})
            for field, value in (suggestions or {}).items():
                if not value:
                    continue
                if not proposed.get(field):
                    proposed[field] = value
                    findings["filled"][field] = source
                elif field in ("name", "legal_address") and name_score(proposed[field], value) < 0.65:
                    findings["conflicts"].append(f"{field.replace('_', ' ').title()} differs from {source}: {value}")

    if country == "BR":
        for cnpj in cnpjs:
            url = f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
            try:
                record = json_fetcher(url)
                address = ", ".join(str(part) for part in (
                    record.get("logradouro"), record.get("numero"), record.get("bairro"),
                    record.get("municipio"), record.get("uf"), record.get("cep")) if part)
                qsa = record.get("qsa") or []
                managers = ", ".join(f"{person.get('nome_socio')} ({person.get('qualificacao_socio')})"
                                    for person in qsa if person.get("nome_socio") and
                                    any(term in folded(person.get("qualificacao_socio") or "")
                                        for term in ("diretor", "administrador", "presidente")))
                owners = ", ".join(f"{person.get('nome_socio')} ({person.get('qualificacao_socio')})"
                                  for person in qsa if person.get("nome_socio") and
                                  any(term in folded(person.get("qualificacao_socio") or "")
                                      for term in ("socio", "acionista")))
                add_match("BrasilAPI / Receita Federal data", url, record.get("razao_social") or "",
                          record.get("cnpj") or "", "BR", [record.get("nome_fantasia") or ""],
                          {"short_name": record.get("nome_fantasia") or "", "legal_address": address,
                           "top_management": managers, "key_owners": owners,
                           "primary_industries": record.get("cnae_fiscal_descricao") or ""})
            except Exception as exc:
                findings["errors"].append(f"BrasilAPI {cnpj}: {type(exc).__name__}: {exc}")

    query = (data.get("short_name") or data["name"]).strip()[:100]
    gleif_url = "https://api.gleif.org/api/v1/lei-records?" + urlencode(
        {"filter[fulltext]": query, "page[size]": "10"})
    try:
        records = json_fetcher(gleif_url).get("data", [])
        for item in records[:10]:
            entity = item.get("attributes", {}).get("entity", {})
            if country and entity.get("jurisdiction") != country:
                continue
            legal = (entity.get("legalName") or {}).get("name") or ""
            alternate = [v.get("name", "") for v in (entity.get("transliteratedOtherNames") or []) +
                         (entity.get("otherNames") or [])]
            score = max((name_score(n, candidate) for n in names for candidate in [legal, *alternate]), default=0)
            if score < 0.55:
                continue
            lei = item.get("id") or item.get("attributes", {}).get("lei", "")
            add_match("GLEIF LEI", f"https://api.gleif.org/api/v1/lei-records/{lei}", legal,
                      entity.get("registeredAs") or "", entity.get("jurisdiction") or "", alternate,
                      {"legal_address": address_text(entity.get("legalAddress") or {})})
    except Exception as exc:
        findings["errors"].append(f"GLEIF: {type(exc).__name__}: {exc}")

    # Discovery is useful for smaller companies without LEIs, but never verifies identity.
    news_query = f'"{data["short_name"] or data["name"]}"'
    url, language = search.feed_url(news_query, data["country_registration"])
    try:
        for item in search.parse_feed(feed_fetcher(url), news_query, language, limit=4):
            findings["news"].append({"title": item["title"], "url": item["url"],
                                     "publisher": item["publisher"]})
    except Exception as exc:
        findings["errors"].append(f"Google News: {type(exc).__name__}: {exc}")
    for saved in db.list_companies(None):
        if country_code(saved["country_registration"]) != country:
            continue
        score = max(name_score(name, candidate) for name in names
                    for candidate in (saved["name"], saved.get("short_name", "")))
        if score >= 0.72:
            findings["possible_duplicates"].append({"id": saved["id"], "name": saved["name"],
                                                     "registration_number": saved["registration_number"],
                                                     "name_score": round(score, 2)})
    error = "; ".join(findings["errors"]) if len(findings["errors"]) >= 2 + len(cnpjs) else ""
    return proposed, findings, error
