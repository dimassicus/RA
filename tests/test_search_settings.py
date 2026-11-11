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
    assert page.text.count('role="switch"') == 5
    data = [('use_perplexity', 'on'), ('use_google', 'off'), ('include_country', 'off'), ('include_industry', 'off'), ('adverse_only', 'on'), ('adverse_only', 'off')]
    assert client.post('/setup', data=MultiDict(data)).status_code == 400
    with client.session_transaction() as session:
        token = session['csrf_token']
    form = MultiDict([('csrf_token', token), *data])
    assert client.post('/setup', data=form).status_code == 302
    saved = dict(use_perplexity=True, use_google=False, include_country=False, include_industry=False, adverse_only=True)
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
    settings = dict(use_perplexity=False, use_google=True, include_country=False, include_industry=False, adverse_only=True)
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
    settings = dict(use_perplexity=False, use_google=True, include_country=country, include_industry=industry, adverse_only=adverse)
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


@pytest.mark.parametrize('country,industry', list(product([False, True], repeat=2)))
def test_source_checks_assess_only_enabled_scopes(database, monkeypatch, country, industry):
    search_settings.save(dict(use_perplexity=True, use_google=False, include_country=country, include_industry=industry, adverse_only=False))
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
