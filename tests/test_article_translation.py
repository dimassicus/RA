from copy import deepcopy

import pytest

from ra import article_translation as at
from ra import headline_review, translation
from ra.scoring import score_article
from ra.verification import verify
from test_verification import ARTICLE, NOW, SUBJECT, document


def approved(*args, **kwargs):
    return {'status': 'faithful', 'model': headline_review.QWEN, 'reasoning': 'off', 'issues': []}


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(headline_review, 'readiness', lambda: {'ready': True})
    monkeypatch.setattr(headline_review, 'audit', approved)
    monkeypatch.setattr(headline_review, 'fallback', lambda *a, **kw: ('', 'Offline test'))
    monkeypatch.setattr(translation, 'argos_translate', lambda *a, **kw: (
        'Acme Manufacturing reports net losses of 40 million dollars.', 'Argos test'))


@pytest.mark.parametrize('language', ['ar', 'az', 'el', 'es', 'ru', 'tr', 'uz', 'vi', 'fr'])
def test_all_languages_use_body_not_headline_and_retain_original_evidence(local, language):
    source = document('Original source text.', language=language)
    prepared = at.prepare(source)
    result = verify(prepared, SUBJECT, {**ARTICLE, 'title_en': 'Acme Manufacturing returns to profit.'})
    assert result['status'] == 'supported' and result['score'] == -1
    assert result['document_hash'] == source['sha256'] and result['language'] == language
    assert result['evidence'][0]['original_passages'] == ['Original source text.']
    assert result['translation']['reviewed'] == result['translation']['total'] == 1
    assert score_article({**ARTICLE, 'verification': result}, SUBJECT, NOW)['score_included']


@pytest.mark.parametrize('english', [
    "Acme Manufacturing's supplier files for bankruptcy.",
    'Acme Manufacturing denies bankruptcy reports.',
    'Acme Manufacturing may file for bankruptcy.',
    'Acme Manufacturing files for bankruptcy. The company denied the report.',
    'Acme Manufacturing filed for bankruptcy in 2020.',
    'Acme Manufacturing announces a new chief executive.',
    'Brazil imposes capital controls. Acme Manufacturing operates in Brazil.',
    'Rival Holdings files for bankruptcy. Acme Manufacturing updates its website.',
])
def test_faithful_translation_is_not_company_impact(local, monkeypatch, english):
    monkeypatch.setattr(translation, 'argos_translate', lambda *a, **kw: (english, 'Argos test'))
    result = verify(at.prepare(document('Source passage.', language='ar')), SUBJECT, ARTICLE)
    assert result['status'] != 'supported'
    assert not score_article({**ARTICLE, 'verification': result}, SUBJECT, NOW)['score_included']


@pytest.mark.parametrize('verdict', ['uncertain', 'material_error', 'inconclusive', 'unavailable'])
def test_failed_review_blocks_scoring_and_retry_reuses_other_passages(local, monkeypatch, verdict):
    source = document('First paragraph.\n' + ('Following source words. ' * 70), language='vi')
    calls = []
    def review(text, *args, **kwargs):
        calls.append(text)
        return {**approved(), 'status': verdict} if text.startswith('First') else approved()
    monkeypatch.setattr(headline_review, 'audit', review)
    prepared = at.prepare(source)
    assert ''.join(p['source'] for p in prepared['translation']['parts']) == source['body']
    assert verify(prepared, SUBJECT, ARTICLE)['status'] == 'needs_review'
    faithful = [p['source'] for p in prepared['translation']['parts'] if at.faithful(p)]
    calls.clear()
    monkeypatch.setattr(headline_review, 'audit', lambda text, *a, **kw: (calls.append(text) or approved()))
    retried = at.prepare(prepared)
    assert retried['translation']['status'] == 'faithful'
    assert not set(calls).intersection(faithful)


def test_complete_coverage_cache_binding_and_invalidation(local, monkeypatch):
    body = ('Paragraph with source facts. ' * 95) + ' Final denial must not disappear.'
    prepared = at.prepare(document(body, language='tr'))
    assert ''.join(p['source'] for p in prepared['translation']['parts']) == body
    assert all(len(p['source']) <= 900 for p in prepared['translation']['parts'])
    monkeypatch.setattr(headline_review, 'audit', lambda *a, **kw: pytest.fail('Approved cache should be reused'))
    monkeypatch.setattr(translation, 'argos_translate', lambda *a, **kw: pytest.fail('Approved cache should be reused'))
    assert at.prepare(prepared) == prepared
    for mutation in ('body', 'missing', 'language', 'reviewer', 'reasoning', 'empty', 'version'):
        changed = deepcopy(prepared)
        if mutation == 'body': changed['body'] += ' Extra denial.'
        elif mutation == 'missing': changed['translation']['parts'].pop()
        elif mutation == 'language': changed['language'] = 'az'
        elif mutation == 'reviewer': changed['translation']['parts'][0]['review']['model'] = headline_review.GEMMA
        elif mutation == 'reasoning': changed['translation']['parts'][0]['review']['reasoning'] = 'on'
        elif mutation == 'empty': changed['translation']['parts'][0]['english'] = ''
        elif mutation == 'version': changed['translation']['version'] = 'old'
        assert verify(changed, SUBJECT, ARTICLE)['status'] == 'needs_review'


def test_offline_review_only_unknown_and_native_languages_fail_safely(local, monkeypatch):
    monkeypatch.setattr(headline_review, 'readiness', lambda: {'ready': False, 'message': 'Start LM Studio'})
    monkeypatch.setattr(translation, 'argos_translate', lambda *a, **kw: pytest.fail('No model work expected'))
    for lang in ('en', 'pt'):
        source = document('Acme Manufacturing files for bankruptcy.', language=lang)
        assert at.prepare(source) == source
    for source in (document('Original text.', language='vi'), document('Original text.', language=''),
                   document('x' * 901, language='ar'),
                   document('Original text.', language='el', review_only=True),
                   {'status': 'unavailable', 'reason': 'Paywall'}):
        prepared = at.prepare(source)
        assert verify(prepared, SUBJECT, ARTICLE)['status'] in ('needs_review', 'unavailable')


def test_argos_first_then_fallback_translations_before_reviews(local, monkeypatch):
    calls = []
    def argos(text, lang, **kwargs):
        assert kwargs == {'complete': True}
        calls.append('argos')
        return '', 'No pair'
    def fallback(text, lang, publisher, status, **kwargs):
        assert kwargs == {'article': True} and not publisher
        calls.append('fallback')
        return 'Acme Manufacturing reports net losses of 40 million dollars.', 'Gemma test'
    monkeypatch.setattr(translation, 'argos_translate', argos)
    monkeypatch.setattr(headline_review, 'fallback', fallback)
    monkeypatch.setattr(headline_review, 'audit', lambda *a, **kw: (calls.append('review') or approved()))
    prepared = at.prepare(document('Source words. ' * 150, language='uz'))
    count = len(prepared['translation']['parts'])
    assert calls == ['argos', 'fallback'] * count + ['review'] * count


def test_argos_complete_mode_never_silently_truncates(monkeypatch):
    calls = []
    class Tokenizer:
        def encode(self, text, **kwargs): return list(text)
        def decode_pieces(self, pieces): return ''.join(pieces)
    class Translator:
        def translate_batch(self, tokens, **kwargs):
            calls.append((tokens, kwargs))
            return [type('Result', (), {'hypotheses': [tokens[0]]})()]
    monkeypatch.setattr(translation, '_model', lambda *a: (Tokenizer(), Translator(), ''))
    source = 'Source with no omitted ending. ' * 20
    assert translation.argos_translate(source, 'es', complete=True)[0] == source.strip()
    assert calls[0][1]['max_input_length'] == 0 and calls[0][1]['max_decoding_length'] == 2048
    assert not translation.argos_translate('x' * 1001, 'es', complete=True)[0]
    assert len(calls) == 1


def test_rejected_argos_uses_separate_fallback_and_fresh_review(local, monkeypatch):
    monkeypatch.setattr(translation, 'argos_translate', lambda *a, **kw: ('The company reported net damages.', 'Argos offline model'))
    english = 'Acme Manufacturing declared a net loss of 40 million dollars.'
    monkeypatch.setattr(headline_review, 'fallback', lambda *a, **kw: (english, headline_review.GEMMA))
    calls = []
    def review(source, language, candidate, **kwargs):
        calls.append(candidate)
        return approved() if candidate == english else {**approved(), 'status': 'material_error'}
    monkeypatch.setattr(headline_review, 'audit', review)
    prepared = at.prepare(document('Original financial statement.', language='el'))
    assert len(calls) == 2
    assert prepared['translation']['parts'][0]['rejected_translation']['english'] == calls[0]
    assert verify(prepared, SUBJECT, ARTICLE)['score'] == -1
    monkeypatch.setattr(headline_review, 'audit', lambda *a, **kw: {**approved(), 'status': 'uncertain'})
    assert verify(at.prepare(document('Different financial statement.', language='el')), SUBJECT, ARTICLE)['status'] == 'needs_review'
