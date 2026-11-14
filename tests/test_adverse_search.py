import re

import pytest

from ra import keywords, risk_factors
from ra.adverse_terms import RELATED_PHRASES, PERSON_TITLES
from ra.aliases import headline_identity, normalized, related_names
from ra.relevance import classify, case_context
from ra.search import adverse_searches, company_search_plan, query_groups
from ra.search_settings import adverse_lead
from ra.verification import verify
from test_score_store import database


COMPANY = dict(name="Acme Manufacturing", country_registration="Brazil", aliases="Acme Metals",
               top_management="Jane Smith (CEO, director); John Adams (CFO)",
               key_owners="Alex White (main shareholder); Acme Holdings")


@pytest.fixture(autouse=True)
def default_catalog(monkeypatch):
    rows = keywords.defaults()
    monkeypatch.setattr(keywords, "_rows", rows)
    monkeypatch.setattr(keywords, "_rules", keywords.compile_terms(rows))


def test_all_sixty_families_and_expansions_match_without_duplicate_seed_rows():
    assert len(RELATED_PHRASES) == 60
    rows = keywords.defaults()
    assert len({normalized(row["english"]) for row in rows}) == len(rows)
    for term, phrases in RELATED_PHRASES.items():
        for phrase in (term, *phrases):
            assert any(hit[0] == term and hit[2] == "adverse" for hit in keywords.matches(phrase)), (term, phrase)
    for title, variants in PERSON_TITLES.items():
        for phrase in (title, *variants):
            assert any(hit[0] == title and hit[2] == "neutral" for hit in keywords.matches(phrase))


def test_enabled_and_sentiment_settings_control_expansions(monkeypatch):
    row = dict(english="drug trafficking", direction="adverse", enabled=False, translations={"pt": "tráfico de drogas"})
    monkeypatch.setattr(keywords, "_rows", [row])
    monkeypatch.setattr(keywords, "_rules", keywords.compile_terms([row]))
    assert not keywords.matches("drug dealing")
    assert not keywords.adverse_search_terms("en")
    row.update(enabled=True, direction="neutral")
    monkeypatch.setattr(keywords, "_rules", keywords.compile_terms([row]))
    assert keywords.matches("drug dealing")[0][2] == "neutral"
    assert not keywords.adverse_search_terms("en")
    row["direction"] = "adverse"
    assert keywords.adverse_search_terms("pt") == ["tráfico de drogas"]
    assert not keywords.adverse_search_terms("ja")


def test_queries_cover_every_phrase_and_people_without_requiring_company_name():
    plan = adverse_searches(COMPANY)
    english = [q for q, edition in plan if edition == ""]
    assert len(plan) == len(set(plan))
    assert max(map(len, english)) < 700
    assert any('"Jane Smith"' in q and '"smuggling"' in q for q in english)
    assert any('"Alex White"' in q and '"drug dealing"' in q for q in english)
    # Each name is an OR alternative, not an additional mandatory company filter.
    assert any('"Acme Manufacturing" OR "Acme Metals" OR "Jane Smith"' in q for q in english)
    quoted = {phrase for q in english for phrase in re.findall(r'"([^\"]+)"', q)}
    for term, phrases in RELATED_PHRASES.items():
        for phrase in (term, *phrases):
            assert any(re.search(r"(?<!\w)" + re.escape(q.casefold()) + r"(?!\w)", phrase.casefold())
                       for q in quoted), phrase
    for title in ("CEO", "main shareholder", "beneficial owner", "chairman"):
        assert any(f'"{title}"' in q and '"Acme Manufacturing"' in q for q in english)


def test_criminal_and_title_searches_do_not_depend_on_governance_change_factors(monkeypatch):
    monkeypatch.setattr(risk_factors, "_rows", [])
    plan = company_search_plan(COMPANY)
    assert any('"drug dealing"' in q for q, _ in plan)
    assert any('"CEO"' in q for q, _ in plan)
    assert not any("devaluation" in q or "capital controls" in q for q, _ in plan)


def test_local_language_and_english_searches_and_titles(monkeypatch):
    monkeypatch.setattr(keywords, "_rows", [
        dict(english="drug trafficking", enabled=True, direction="adverse", translations={"pt": "tráfico de drogas"}),
        dict(english="chief executive officer", enabled=True, direction="neutral", translations={"pt": "diretor executivo"}),
    ])
    plan = adverse_searches(COMPANY)
    assert any('"tráfico de drogas"' in q and edition == "Brazil" for q, edition in plan)
    assert any('"diretor executivo"' in q and edition == "Brazil" for q, edition in plan)
    assert any('"drug dealing"' in q and edition == "" for q, edition in plan)


def test_long_lists_are_batched_without_losing_names_or_terms():
    terms = [f'"Person {i}"' for i in range(30)]
    batches = list(query_groups(terms, limit=4, max_chars=65))
    assert [name for batch in batches for name in re.findall(r'"[^\"]+"', batch)] == terms
    assert all(len(batch) <= 67 for batch in batches)
    names = related_names(dict(COMPANY, top_management="CEO; CEO: Jane Smith; Jane Smith (CEO); John Adams · CFO"))
    assert names[:2] == ["Jane Smith", "John Adams"]
    assert "CEO" not in names
    assert not headline_identity(normalized("Jane Smith arrested"), COMPANY)


@pytest.mark.parametrize("title,stage", [
    ("Acme Manufacturing CEO accused of drug dealing", "allegation"),
    ("Acme Manufacturing under investigation for smuggling", "investigation"),
    ("Jane Smith arrested for smuggling", "arrest"),
    ("Acme Manufacturing main shareholder charged with bribery", "charges"),
    ("Acme Manufacturing CEO convicted of fraud", "conviction"),
    ("Acme Manufacturing denies money laundering", "denial"),
    ("Acme Manufacturing owner acquitted of drug trafficking", "cleared or resolved"),
    ("Acme Manufacturing was a victim of fraud", "victim or witness"),
])
def test_case_stages_stay_visible_without_becoming_company_findings(title, stage):
    article = dict(title=title, language="en", url="https://publisher.example/case")
    assert stage in case_context(title)
    assert adverse_lead(article, COMPANY)
    assert classify(article, COMPANY)["relevance"] == "review"
    result = verify(dict(status="available", language="en", body=title + "."), COMPANY, article)
    assert result["status"] == "needs_review"
    assert stage in result["case_context"]
    assert "score" not in result


def test_person_only_source_remains_reviewable_but_namesake_is_not_company_evidence():
    article = dict(title="Jane Smith faces drug trafficking allegations", language="en", url="https://publisher.example/case")
    result = verify(dict(status="available", language="en", body=article["title"]), COMPANY, article)
    assert result["status"] == "needs_review" and "relationship" in result["reason"]
    unrelated = verify(dict(status="available", language="en", body="Other Person convicted of fraud."), COMPANY, article)
    assert unrelated["status"] == "unmatched"


def test_new_adverse_event_is_not_hidden_by_separate_positive_event():
    assert adverse_lead(dict(title="Acme Manufacturing returns to profit amid drug trafficking allegations", language="en"), COMPANY)
    assert not adverse_lead(dict(title="Acme Manufacturing exits from bankruptcy", language="en"), COMPANY)
    assert not adverse_lead(dict(title="Acme Manufacturing CEO attends annual meeting", language="en"), COMPANY)


def test_database_seed_preserves_existing_rows_translations_and_disabled_settings(database):
    database.execute("INSERT INTO monitor_keywords(english,direction,enabled,translations) "
                     "VALUES ('bankruptcy','adverse',false,'{\"pt\":\"falência\"}'), "
                     "('Asset Seizure','neutral',true,'{}')")
    before = database.execute("SELECT * FROM monitor_keywords ORDER BY id").fetchall()
    keywords.seed(database)
    first = database.execute("SELECT * FROM monitor_keywords ORDER BY id").fetchall()
    keywords.seed(database)
    assert database.execute("SELECT * FROM monitor_keywords ORDER BY id").fetchall() == first
    assert first[:2] == before
    assert len({normalized(row['english']) for row in first}) == len(first)
    assert any(row['english'] == 'drug trafficking' and row['direction'] == 'adverse' for row in first)
    rules = keywords.compile_terms(first[:2])
    assert any(term == 'Asset Seizure' and direction == 'neutral' and pattern.search('property attachment')
               for term, _, direction, pattern, _ in rules)


def test_person_only_leads_are_retained_in_company_reports():
    from datetime import datetime, timezone
    from ra.company_report import relevant_sources
    now = datetime.now(timezone.utc)
    article = dict(id=1, title="Jane Smith accused of drug dealing", title_en="", language="en",
                   published_at=now, source_kind="news", verification={"status": "needs_review"},
                   exposure_paths=[])
    source, = relevant_sources(COMPANY, [article], now)
    assert source['branches'] == ['Company']


def test_keyword_page_exposes_phrases_and_title_scope(database):
    from ra.app import create_app
    keywords.seed(database)
    client = create_app().test_client()
    page = client.get('/keywords')
    assert page.status_code == 200
    assert b'drug dealing' in page.data and b'company-linked titles' in page.data


def test_reviewed_phrase_translations_reach_company_and_person_searches(database, monkeypatch):
    from ra.app import create_app
    from ra.local_models import GEMMA, QWEN

    keywords.seed(database)
    keywords.seed(database)
    rows = keywords.records()
    phrases = keywords.active_phrases(rows)
    assert len(phrases) == sum(map(len, keywords.PHRASES.values()))
    assert not {p["id"] for p in phrases} & {row["id"] for row in rows}

    candidates = {"falsified accounts": "contas falsificadas", "fabricated revenue": "receita inventada",
                  "main shareholder": "acionista principal", "managing director": "diretor-geral"}
    batch = [p for p in phrases if p["english"] in candidates]
    reviewers = []
    def model(prompt, data, schema, *, model):
        if "terms" in data:
            assert model == GEMMA
            return candidates if isinstance(data["terms"], list) else {"fabricated revenue": "candidate rejected again"}
        reviewers.append(model)
        assert "accounting fraud" in data["intended_meanings"]["falsified accounts"]
        return {term: {"approved": term != "fabricated revenue", "reason": "", "correction": ""}
                for term in data["translations"]}
    monkeypatch.setattr(keywords, "model_json", model)
    monkeypatch.setattr(keywords, "last_error", "")
    keywords.translate_batch(batch, "pt")
    assert reviewers[0] == QWEN
    keywords.reload_rules()
    plan = [q for q, edition in company_search_plan(COMPANY) if edition == "Brazil"]
    for name in ["Acme Manufacturing", "Jane Smith", "Alex White"]:
        assert any(f'"{name}"' in q and '"contas falsificadas"' in q for q in plan)
    for title in ["acionista principal", "diretor-geral"]:
        assert all('"Acme Manufacturing"' in q for q in plan if f'"{title}"' in q)
        assert any(f'"{title}"' in q for q in plan)
    assert not any("receita inventada" in q or "candidate rejected" in q for q in plan)
    assert ("accounting fraud", "financial crime", "adverse") in keywords.matches("contas falsificadas", "pt-BR")
    assert not keywords.matches("contas falsificadas", "es")
    attempts = keywords.translation_feedback(batch, "pt", False)
    assert not attempts["fabricated revenue"]["approved"] and attempts["main shareholder"]["approved"]

    monkeypatch.setattr(keywords, "languages", lambda: ["pt"])
    client = create_app().test_client()
    page = client.get("/keywords").get_data(as_text=True)
    assert "contas falsificadas" in page and "receita inventada" not in page
    totals = client.get("/api/translations/status?factors=0").get_json()
    assert totals["total"] == len(rows) + len(phrases)
    assert totals["saved"] == 3 + sum("pt" in row["translations"] for row in rows)

    parent = next(row for row in rows if row["english"] == "accounting fraud")
    keywords.save(parent["id"], parent["english"], "adverse", False, parent["english"])
    assert "contas falsificadas" not in keywords.adverse_search_terms("pt")
    assert not keywords.matches("contas falsificadas", "pt")
    assert not any(p["family"] == "accounting fraud" for p in keywords.active_phrases(keywords.records()))


def test_company_phrase_search_includes_registration_and_operating_languages(monkeypatch):
    row = dict(english="accounting fraud", enabled=True, direction="adverse", translations={},
               phrases=[dict(english="fabricated revenue", translations={"tt": "ялган керем", "zh": "虚构收入"})])
    monkeypatch.setattr(keywords, "_rows", [row])
    subject = dict(COMPANY, country_registration="Russian Federation", primary_countries="China")
    plan = adverse_searches(subject)
    assert any(edition == "Russian Federation" and '"ялган керем"' in q and '"Jane Smith"' in q for q, edition in plan)
    assert any(edition == "china" and '"虚构收入"' in q and '"Alex White"' in q for q, edition in plan)


def test_worker_schedules_each_enabled_phrase_for_each_missing_language(database, monkeypatch):
    keywords.seed(database)
    database.execute("UPDATE monitor_keywords SET enabled = (english = 'accounting fraud')")
    database.execute("UPDATE keyword_phrases SET translations='{\"pt\":\"contas falsificadas\"}' "
                     "WHERE english='falsified accounts'")
    monkeypatch.setattr(keywords, "_requested", True)
    monkeypatch.setattr(keywords, "_running", True)
    monkeypatch.setattr(keywords, "_progress", ("", 0))
    monkeypatch.setattr(keywords, "last_error", "")
    monkeypatch.setattr(keywords, "languages", lambda: ["pt", "ru"])
    monkeypatch.setattr(risk_factors, "languages", lambda: [])
    monkeypatch.setattr(risk_factors, "expand_queries", lambda code: None)
    work = []
    def collect(rows, code, *, factors):
        assert not factors
        work.extend((row["english"], code) for row in rows)
        return iter(())
    monkeypatch.setattr(keywords, "_translation_steps", collect)
    keywords._run_passes()
    expected = {(term, code) for term in ("accounting fraud", *RELATED_PHRASES["accounting fraud"])
                for code in ("pt", "ru")} - {("falsified accounts", "pt")}
    assert len(work) == len(expected) and set(work) == expected
