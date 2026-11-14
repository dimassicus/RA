from datetime import datetime, timedelta, timezone
from itertools import product
import re

import pytest
from werkzeug.datastructures import MultiDict

from ra import db, keywords, risk_factors, score_store, search_settings, verification_store, worker
from ra.app import create_app
from test_score_store import database, seed


@pytest.mark.parametrize('title,expected', [
    ('Acme Manufacturing files for bankruptcy', True),
    ('Acme Manufacturing exits from bankruptcy', False),
    ('Acme Manufacturing reduces losses', False),
    ('Acme Manufacturing returns to profit', False),
    ('Acme Manufacturing reports routine annual meeting', False),
    ('Acme Manufacturing wins a contract but faces a lawsuit', True),
    ('Acme Manufacturing may default on debt', True),
])
def test_adverse_screen_keeps_risk_leads_without_reversing_recoveries(title, expected):
    assert search_settings.adverse_lead(dict(title=title, language='en'), {'name': 'Acme Manufacturing'}) is expected


def test_adverse_screen_uses_native_custom_keywords_and_translation(monkeypatch):
    monkeypatch.setattr(keywords, '_rules', keywords.compile_terms([
        dict(english='export prohibition', direction='adverse', enabled=True, translations={'ru': 'запрет экспорта'}),
        dict(english='expansion', direction='favorable', enabled=True, translations={'ru': 'расширение'}),
    ]))
    subject = {'name': 'Acme'}
    assert search_settings.adverse_lead(dict(title='Acme: запрет экспорта', language='ru'), subject)
    assert not search_settings.adverse_lead(dict(title='Acme: расширение', language='ru'), subject)
    assert search_settings.adverse_lead(dict(title='Original text', title_en='Acme faces a lawsuit', language='ru'), subject)
    assert search_settings.for_run({}) == search_settings.DEFAULTS | {"use_google": False}


def test_setup_persists_all_switches_and_rejects_invalid_forms(database):
    assert search_settings.read() == search_settings.DEFAULTS
    client = create_app().test_client()
    page = client.get('/setup')
    assert page.status_code == 200
    assert page.text.count('role="switch"') == 6
    data = [('use_google_news_rss', 'off'), ('use_perplexity', 'on'), ('use_google', 'off'), ('include_country', 'off'), ('include_industry', 'off'), ('adverse_only', 'on'), ('adverse_only', 'off')]
    assert client.post('/setup', data=MultiDict(data)).status_code == 400
    with client.session_transaction() as session:
        token = session['csrf_token']
    form = MultiDict([('csrf_token', token), *data])
    assert client.post('/setup', data=form).status_code == 302
    saved = dict(use_google_news_rss=False, use_perplexity=True, use_google=False, include_country=False, include_industry=False, adverse_only=True)
    assert search_settings.read() == saved
    # A new app/session reads PostgreSQL, not a session or process-local default.
    fresh = create_app().test_client().get('/setup').text
    for key, value in saved.items():
        control = re.search(r'<input type="checkbox"[^>]*name="' + key + r'"[^>]*>', fresh)[0]
        assert ('checked' in control) == value
    for bad in ({'adverse_only': 'yes'}, {'include_country': ''}):
        assert client.post('/setup', data={'csrf_token': token, **{k: 'on' for k in saved}, **bad}).status_code == 400
        assert search_settings.read() == saved
    with pytest.raises(ValueError):
        search_settings.save(saved | {'adverse_only': 'on'})
    assert client.post('/setup', data={'csrf_token': token, **{k: 'off' for k in saved}}).status_code == 302
    assert search_settings.read() == dict.fromkeys(saved, False)
    assert database.execute('SELECT count(*) AS n FROM search_runs').fetchone()['n'] == 0


@pytest.mark.parametrize('scope', ['all', 'selected', 'one_off'])
def test_every_search_route_snapshots_current_global_settings(database, monkeypatch, scope):
    company, original, article = seed(database)
    score_store.review_score(article['id'], -1, True, 'Analyst decision preserved')
    before = db.get_results(original)
    settings = dict(use_google_news_rss=False, use_perplexity=False, use_google=True, include_country=False, include_industry=False, adverse_only=True)
    search_settings.save(settings)
    queued = []
    monkeypatch.setattr(worker, 'enqueue', queued.append)
    client = create_app().test_client()
    with client.session_transaction() as session:
        session['csrf_token'] = 'token'
    form = dict(csrf_token='token', company_id=company['id'], name='One off', country_registration='Brazil')
    response = client.post('/search/' + scope.replace('_', '-'), data=form)
    if scope == 'one_off':
        assert response.status_code == 410
        assert queued == []
        return
    assert response.status_code == 302
    new = db.get_run(queued[0])
    assert new['search_settings'] == settings
    search_settings.save(search_settings.DEFAULTS)
    assert db.get_run(new['id'])['search_settings'] == settings
    assert db.get_results(original) == before
    assert db.get_run(original)['search_settings'] == search_settings.DEFAULTS


@pytest.mark.parametrize('country,industry,adverse', list(product([False, True], repeat=3)))
def test_worker_settings_cover_branches_sources_and_retained_counts(database, monkeypatch, country, industry, adverse):
    settings = dict(use_google_news_rss=True, use_perplexity=False, use_google=True, include_country=country, include_industry=industry, adverse_only=adverse)
    search_settings.save(settings)
    subject = dict(name='Acme Manufacturing', country_registration='Brazil', primary_industries='fertilizer')
    run_id = db.create_run('one_off', [subject])
    # Changing the global defaults cannot change a run halfway through.
    search_settings.save(search_settings.DEFAULTS)
    monkeypatch.setattr(worker, 'plan_subjects', lambda subjects, run_id: subjects)
    monkeypatch.setattr(worker, 'translate_text', lambda title, *args: (title, 'English'))
    monkeypatch.setattr(worker, 'detect_language', lambda *args: 'en')
    monkeypatch.setattr(worker.time, 'sleep', lambda *args: None)
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    def article(title, service, kind='news'):
        return dict(title=title, url=f'https://example.test/{service}/{title}', published_at=now,
                    publisher='Test', language='en', query='test', signal_terms=[],
                    source_kind=kind, discovery_service=service)
    articles = [article('Acme Manufacturing ' + event, service, kind)
                for service, kind in [('Google News RSS', 'news'), ('GNews', 'news'),
                                      ('TheNewsAPI', 'news'), ('Issuer disclosures', 'official')]
                for event in ('files for bankruptcy', 'returns to profit', 'annual meeting')]
    called = []
    def topic_search(branch, topic, **options):
        called.append(branch)
        return [(article('Brazil fertilizer plant shutdown', branch, 'context'), 'supply disruption')]
    worker.process_run(run_id, searcher=lambda _: (articles, []), topic_searcher=topic_search,
                       roster_check=lambda *args: None)
    assert set(called) == ({'country'} if country else set()) | ({'sector'} if industry else set())
    results = db.get_results(run_id)
    assert len(results) == 12 + int(country) + int(industry)
    assert db.get_run(run_id)['result_count'] == len(results)
    assert database.execute('SELECT count(*) AS n FROM verification_jobs').fetchone()['n'] == len(results)
    if not country:
        assert all(not path.startswith('Country') for row in results for path in row['exposure_paths'])
    if not industry:
        assert all(not path.startswith('Sector') for row in results for path in row['exposure_paths'])


def test_intake_news_uses_global_adverse_filter(database, monkeypatch):
    from ra import preliminary, search
    search_settings.save(search_settings.DEFAULTS | {'adverse_only': True})
    monkeypatch.setattr(search, 'parse_feed', lambda *args, **kwargs: [
        dict(title=title, url='https://example.test/' + str(i), publisher='Test', language='en')
        for i, title in enumerate(('Acme bankruptcy', 'Acme returns to profit', 'Acme annual meeting'))])
    _, findings, _ = preliminary.research_company(
        dict(name='Acme', short_name='Acme', country_registration='Brazil'),
        json_fetcher=lambda url: {}, feed_fetcher=lambda url: b'')
    assert [item['title'] for item in findings['news']] == ['Acme bankruptcy']
    search_settings.save(search_settings.DEFAULTS | {'use_google_news_rss': False})
    _, findings, _ = preliminary.research_company(
        dict(name='Acme', short_name='Acme', country_registration='Brazil'),
        json_fetcher=lambda url: {}, feed_fetcher=lambda url: pytest.fail('RSS disabled'))
    assert findings['news'] == []


@pytest.mark.parametrize('country,industry', list(product([False, True], repeat=2)))
def test_source_checks_assess_only_enabled_scopes(database, monkeypatch, country, industry):
    search_settings.save(dict(use_google_news_rss=True, use_perplexity=True, use_google=False, include_country=country, include_industry=industry, adverse_only=False))
    _, _, article = seed(database)
    search_settings.save(search_settings.DEFAULTS)
    factors = [dict(id=i, scope=scope, enabled=True) for i, scope in enumerate(('company', 'country', 'sector'))]
    monkeypatch.setattr(risk_factors, 'records', lambda: factors)
    seen = []
    monkeypatch.setattr(risk_factors, 'cached_assess', lambda article, document, subject, rows: seen.extend(rows) or {})
    verification_store.verify_article(article['id'], fetcher=lambda url: dict(status='available', language='en',
        body='Acme Manufacturing has filed for bankruptcy.', sha256='body', final_url=url, method='test'))
    assert {r['scope'] for r in seen} == {'company'} | ({'country'} if country else set()) | ({'sector'} if industry else set())


def test_adverse_view_retains_positive_source_and_analyst_decision(database, monkeypatch):
    search_settings.save(search_settings.DEFAULTS | {'adverse_only': True})
    _, run_id, article = seed(database)
    monkeypatch.setattr(risk_factors, 'records', lambda: [])
    verification_store.verify_article(article['id'], fetcher=lambda url: dict(status='available', language='en',
        body='Acme Manufacturing returns to profit.', sha256='recovery', final_url=url, method='test'))
    saved = score_store.get_article(article['id'])
    assert saved['score'] == 1 and saved['score_included']
    score_store.review_score(article['id'], 1, True, 'Analyst confirms recovery despite misleading headline')
    assert score_store.refresh_scores(run_id)['articles_updated'] == 0
    assert score_store.get_article(article['id'])['score_included']


def test_role_only_enforcement_hits_reach_source_review(database, monkeypatch):
    from ra import event_analysis, news_apis, scoring, verification
    company, run_id, _ = seed(database)
    company = dict(company, name="Kazakh Railways", aliases="КТЖ", country_registration="Kazakhstan")
    translated = []
    monkeypatch.setattr(worker, "translate_text", lambda title, *args: (translated.append(title) or "", "unavailable"))
    data = dict(title="В Казахстане задержали глав железной дороги и фонда", language="ru",
                description="По обвинению в коррупции", snippet="Следствие продолжается.",
                keywords="Казахстан, КТЖ", url="https://news.test/arrest", source="news.test",
                published_at="2026-11-12T07:28:13Z")
    ordinary = dict(data, title="Railway timetable", keywords="", description="", snippet="",
                    url="https://news.test/timetable")
    unrelated = dict(data, title="В Казахстане задержали чиновника", keywords="Казахстан, коррупция",
                     url="https://news.test/unrelated")
    hits = news_apis.parse_articles("thenewsapi", {"data": [data, ordinary, unrelated]}, "КТЖ company query")
    count = worker._save_leads(run_id, company, str(company['id']), hits,
                               dict(include_country=False, include_industry=False), lambda *args: None,
                               set(), {}, connection=database, queue_checks=True)
    assert count == 1 and translated == [data["title"]]
    row = database.execute("SELECT * FROM search_results WHERE url=%s", (data["url"],)).fetchone()
    assert row["source_kind"] == "identity_review"
    assert all(data[k] in row["discovery_summary"] for k in ("description", "snippet", "keywords"))
    assert database.execute("SELECT status FROM verification_jobs WHERE result_id=%s", (row["id"],)).fetchone()
    row.update(event_draft=dict(event=1, sentiment=-1), report_selected=True)
    row.update(scoring.score_article(row, company))
    assert event_analysis.candidate(row) is False
    assert not row["score_included"] and not event_analysis.triage_candidate(row)
    doc = dict(status="available", language="en", body=f"The chairman of {company['name']} was arrested.")
    row["verification"] = verification.verify(doc, company, row)
    assert row["verification"]["status"] == "needs_review" and event_analysis.triage_candidate(row)
    assert not scoring.score_article(row, company)["score_included"]
    row["verification"] = verification.verify(dict(doc, body="The chairman of another railway was arrested."), company, row)
    assert not event_analysis.triage_candidate(row)


def test_partial_run_shows_coverage_and_unresolved_enforcement_in_summary(database):
    company, run_id, row = seed(database, title="Railway chief detained")
    database.execute("UPDATE search_runs SET status='partial',error_count=1,event_analysis_status='complete' WHERE id=%s", (run_id,))
    database.execute("INSERT INTO search_errors(run_id,subject_name,message) VALUES (%s,%s,'Provider timed out')", (run_id, company['name']))
    database.execute("UPDATE search_results SET source_kind='identity_review' WHERE id=%s", (row['id'],))
    page = create_app().test_client().get(f"/runs/{run_id}").text
    summary = page.split('id="ai-summary"', 1)[1].split('</section>', 1)[0]
    assert 'Incomplete search coverage.' in summary and 'href="#source-errors"' in summary
    assert 'Enforcement leads awaiting a company identity check (1)' in summary
    assert '#source-evidence' in summary and 'Railway chief detained' in summary
    assert 'No company events found' not in summary


def test_recovered_source_alias_is_scoped_to_evidence_and_one_issuer():
    profile = dict(id=13, name="Company in the original register", aliases="Original Alias")
    other = dict(id=14, name="Another company", aliases="")
    run = dict(subjects=[profile, other], search_settings={"evidence_aliases": {
        "13": [{"name": "Confirmed English Name", "url": "https://issuer.test/about"}]}})
    evidence = db.evidence_subject(dict(company_id=13), run)
    assert evidence["aliases"] == "Original Alias\nConfirmed English Name"
    assert profile["aliases"] == "Original Alias"
    assert db.evidence_subject(dict(company_id=14), run) == other


def test_publisher_tag_evidence_never_comes_from_query_or_country_alone():
    target = dict(name='Kazakh Railways', aliases='КТЖ', country_registration='Kazakhstan')
    hit = dict(title='Railway chairman arrested', query='"КТЖ" Kazakhstan corruption',
               discovery_service='TheNewsAPI', language='en')
    for tags in ('Казахстан, коррупция', 'КТЖ', 'Другие КТЖ Новости', 'Новости'):
        assert not search_settings.enforcement_candidate(dict(hit, publisher_keywords=tags), target)
    assert search_settings.enforcement_candidate(dict(hit, publisher_keywords=['Казахстан', 'КТЖ']), target)
    assert not search_settings.enforcement_candidate(dict(hit, publisher_keywords=['Казахстан', 'КТЖ'],
                                                          exposure_paths=['country']), target)
