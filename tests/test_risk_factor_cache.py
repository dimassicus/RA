import copy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from ra import headline_review, risk_factors


@pytest.fixture
def cache(monkeypatch):
    saved, calls = [], []

    class Connection:
        def execute(self, query, params):
            assert "risk_factor_review->>'status'='checked'" in query
            rows = [row for row in saved if row.get('input_hash') == params[0]
                    and row.get('status') == 'checked']
            return SimpleNamespace(fetchone=lambda: {'risk_factor_review': rows[-1]} if rows else None)

    @contextmanager
    def connect():
        yield Connection()

    def assess(*args):
        calls.append(args)
        return {'status': 'checked', 'coverage': 'headline and source text',
                'matches': [{'factor': 'Management change', 'kind': 'partial',
                             'quote': 'Original evidence', 'connection': 'Some evidence.'}]}

    monkeypatch.setattr(risk_factors.db, 'connect', connect)
    monkeypatch.setattr(risk_factors, 'assess', assess)
    inputs = [
        {'title': 'Company update - Publisher', 'publisher': 'Publisher'},
        {'status': 'available', 'body': 'Original evidence', 'language': 'en', 'sha256': 'untrusted hint'},
        {'name': 'Acme', 'aliases': 'Acme Ltd', 'country_registration': 'Brazil',
         'primary_countries': 'Brazil', 'primary_industries': 'Fertilizer'},
        [{'id': 1, 'english': 'Management change', 'scope': 'company'},
         {'id': 2, 'english': 'Capital controls', 'scope': 'country'}],
    ]
    return SimpleNamespace(saved=saved, calls=calls, inputs=inputs)


def test_identical_input_reuses_checked_assessment_without_sharing_mutable_data(cache):
    original = copy.deepcopy(cache.inputs)
    first = risk_factors.cached_assess(*cache.inputs)
    assert first['version'] == risk_factors.ASSESSMENT_VERSION
    assert first['model'] == headline_review.QWEN
    cache.saved.append(copy.deepcopy(first))  # Persistence is owned by verification_store.
    second = risk_factors.cached_assess(*cache.inputs)
    assert second == first and len(cache.calls) == 1
    second['matches'][0]['quote'] = 'Changed by caller'
    assert cache.saved[0]['matches'][0]['quote'] == 'Original evidence'
    assert cache.inputs == original


@pytest.mark.parametrize('section,key,value', [
    (0, 'title', 'Different company update - Publisher'),
    (1, 'body', 'Changed source with the same supplied hash'),
    (1, 'status', 'unavailable'),
    (1, 'language', 'pt'),
    (1, 'review_only', True),
    (2, 'name', 'Other company'),
    (2, 'aliases', 'Different alias'),
    (2, 'country_registration', 'Greece'),
    (2, 'primary_countries', 'Turkey'),
    (2, 'primary_industries', 'Aluminium'),
    (3, 'id', 3),
    (3, 'english', 'Ownership change'),
    (3, 'scope', 'sector'),
])
def test_cache_invalidates_when_actual_assessment_inputs_change(cache, section, key, value):
    cache.saved.append(risk_factors.cached_assess(*cache.inputs))
    if section == 3:
        cache.inputs[3][0][key] = value
    else:
        cache.inputs[section][key] = value
    result = risk_factors.cached_assess(*cache.inputs)
    assert len(cache.calls) == 2
    assert result['input_hash'] != cache.saved[0]['input_hash']


@pytest.mark.parametrize('change', ['protocol', 'model', 'enabled_factors', 'factor_order'])
def test_cache_invalidates_protocol_model_and_factor_configuration(cache, monkeypatch, change):
    cache.saved.append(risk_factors.cached_assess(*cache.inputs))
    if change == 'protocol':
        monkeypatch.setattr(risk_factors, 'ASSESSMENT_VERSION', 'future-protocol')
    elif change == 'model':
        monkeypatch.setattr(headline_review, 'QWEN', 'different-model')
    elif change == 'enabled_factors':
        cache.inputs[3].pop()
    else:
        cache.inputs[3].reverse()
    assert risk_factors.cached_assess(*cache.inputs)['input_hash'] != cache.saved[0]['input_hash']
    assert len(cache.calls) == 2


def test_publisher_suffix_and_source_url_do_not_change_actual_model_input(cache):
    cache.saved.append(risk_factors.cached_assess(*cache.inputs))
    cache.inputs[0].update(title='Company update - Other Publisher', publisher='Other Publisher')
    cache.inputs[1]['final_url'] = 'https://different.test/same-text'
    assert risk_factors.cached_assess(*cache.inputs) == cache.saved[0]
    assert len(cache.calls) == 1


@pytest.mark.parametrize('status', ['inconclusive', 'incomplete'])
def test_incomplete_reviews_neither_reuse_nor_publish_a_cache_key(cache, monkeypatch, status):
    checked = risk_factors.cached_assess(*cache.inputs)
    cache.saved.append({**checked, 'status': status})
    monkeypatch.setattr(risk_factors, 'assess', lambda *args: {'status': status, 'matches': []})
    result = risk_factors.cached_assess(*cache.inputs)
    assert result == {'status': status, 'matches': []}


def test_legacy_checked_results_without_fingerprint_are_not_reused(cache):
    cache.saved.append({'status': 'checked', 'matches': []})
    assert risk_factors.cached_assess(*cache.inputs)['input_hash']
    assert len(cache.calls) == 1


def test_no_factors_does_not_query_database_or_models(cache, monkeypatch):
    def unexpected():
        raise AssertionError('Database access was not needed')
    monkeypatch.setattr(risk_factors.db, 'connect', unexpected)
    cache.inputs[3].clear()
    assert risk_factors.cached_assess(*cache.inputs) == {}
    assert not cache.calls
