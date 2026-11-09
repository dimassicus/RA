"""Meaningful failure and routing tests for the isolated experiment, no models/API."""
from copy import deepcopy
import pytest

from scripts.translation_review.checks import diagnostics, currency_set
from scripts.translation_review.reviewer import CATEGORIES, validate_response
from scripts import evaluate_translation_review as evaluation


def assessment(label='A', verdict='faithful'):
    return {'candidate': label, 'verdict': verdict,
            'coverage': {c: 'preserved' for c in CATEGORIES}, 'issues': []}


def response(*rows, correction=''):
    return {'assessments': list(rows), 'corrected_translation': correction}


def case():
    return {'id': 'independent-fixture', 'kind': 'fictional',
            'source_lang': 'ru', 'target_lang': 'en',
            'source': 'Долг составляет 50 млн EUR.',
            'candidates': {'baseline': 'Debt is EUR 50 million.', 'madlad': 'Debt is EUR 50 million.'},
            'reference': {}}


def test_currencies_and_locale_formats():
    assert currency_set('R $ 172 milhões') == {'BRL'}
    assert currency_set('R$ 172 million and $20 million') == {'BRL','USD'}
    flags = diagnostics('850 tỷ đồng', '850 billion won', 'en')
    assert flags[0]['code'] == 'currency_difference'
    assert flags[0]['source'] == ['VND'] and flags[0]['translation'] == ['KRW']
    assert not diagnostics('R$ 1,1 bilhão', 'R$ 1.1 billion', 'en')
    assert not diagnostics('900 000 евро', 'EUR 900,000', 'en')
    assert currency_set('рублей, rubles, roubles, RUB') == {'RUB'}
    assert currency_set('долл. США') == {'USD'}
    assert any(f['code']=='magnitude_word_difference' for f in
               diagnostics('2 млрд рублей','2 million rubles','en'))
    assert any(f['code']=='possible_numeric_scale_change' for f in
               diagnostics('250.000.002 ευρώ', 'EUR 250,000.002', 'en'))
    assert any(f['code']=='numeric_surface_difference' for f in
               diagnostics('22.000.069', '2.00 22.000.069', 'en'))


def test_spelled_number_is_a_diagnostic_not_a_claim_of_error():
    assert diagnostics('4 дня', 'four days', 'en') == [{'code':'numeric_surface_difference'}]


@pytest.mark.parametrize('failure', ['missing_candidate','duplicate','missing_category','fabricated_quote',
                                    'contradictory','unsupported_error','missing_issue_field'])
def test_invalid_verdicts_fail_closed(failure):
    r = response(assessment())
    if failure == 'missing_candidate': r['assessments'] = []
    if failure == 'duplicate': r['assessments'].append(deepcopy(r['assessments'][0]))
    if failure == 'missing_category': del r['assessments'][0]['coverage']['negation']
    if failure in ('fabricated_quote','missing_issue_field'):
        r['assessments'][0]['issues'] = [{'category':'negation','severity':'wording',
            'source_quote':'invented' if failure=='fabricated_quote' else 'не',
            'translation_quote':'not','explanation':'Example'}]
        if failure == 'missing_issue_field': del r['assessments'][0]['issues'][0]['explanation']
    if failure == 'contradictory': r['assessments'][0]['coverage']['negation'] = 'error'
    if failure == 'unsupported_error': r['assessments'][0]['verdict'] = 'material_error'
    with pytest.raises(ValueError): validate_response(r, 'не платил', {'A':'did not pay'})


def test_quote_validated_but_not_claimed_as_semantic_proof():
    r = response(assessment())
    assert validate_response(r,'не платил',{'A':'did not pay'}) == r


def test_no_model_or_bad_response_cannot_pass(monkeypatch):
    def unavailable(*args, **kwargs): raise TimeoutError('local reviewer unavailable')
    monkeypatch.setattr(evaluation,'review',unavailable)
    r=evaluation.evaluate(case(),'test',11435)
    assert r['status']=='translation_uncertain' and r['selected'] is None
    assert r['review_status']=='failed' and not r['score_eligible']


def test_review_is_blinded_and_baseline_preferred(monkeypatch):
    c = case()
    c['reference']={'baseline':'material'}  # Expected labels must never enter request.
    def reviewer(source, src, dst, candidates, model, port):
        assert all(n in ('A','B') for n in candidates)
        assert 'reference' not in source and 'material' not in str(candidates)
        return response(*(assessment(n) for n in candidates)), {}
    monkeypatch.setattr(evaluation,'review',reviewer)
    r=evaluation.evaluate(c,'test',11435)
    assert r['selected']=='baseline' and not r['score_eligible']
    assert evaluation.summarize([r])['known_bad_selected']==1


def test_ai_cannot_override_currency_gate(monkeypatch):
    c=case();c['source']='850 tỷ đồng';c['source_lang']='vi'
    c['candidates']={'baseline':'850 billion dollars','madlad':'850 billion won'}
    monkeypatch.setattr(evaluation,'review',lambda s,sl,tl,cs,m,p:
                        (response(*(assessment(n) for n in cs)),{}))
    r=evaluation.evaluate(c,'test',11435)
    assert r['selected'] is None and r['status']=='translation_uncertain'


def test_correction_requires_separate_check(monkeypatch):
    c=case();calls=[]
    def reviewer(source,src,dst,candidates,model,port):
        calls.append(candidates)
        if len(calls)==1:
            return response(*(assessment(n,'uncertain') for n in candidates),
                            correction='Debt is EUR 50 million.'),{}
        assert candidates=={'X':'Debt is EUR 50 million.'}
        return response(assessment('X','uncertain')),{}
    monkeypatch.setattr(evaluation,'review',reviewer)
    r=evaluation.evaluate(c,'test',11435)
    assert len(calls)==2 and r['selected'] is None and not r['score_eligible']


def test_correction_cannot_enable_live_scoring(monkeypatch):
    c=case();calls=[]
    def reviewer(source,src,dst,candidates,model,port):
        calls.append(candidates)
        if len(calls)==1:
            return response(*(assessment(n,'uncertain') for n in candidates),
                            correction='Debt is EUR 50 million.'),{}
        return response(assessment('X')),{}
    monkeypatch.setattr(evaluation,'review',reviewer)
    r=evaluation.evaluate(c,'test',11435)
    assert r['selected']=='correction' and r['status']=='shadow_candidate'
    assert not r['score_eligible']


def test_lmstudio_preserves_blinding_and_rejects_truncation(monkeypatch):
    import json
    from scripts.translation_review import lmstudio
    from scripts.translation_review.reviewer import ReviewError, PROMPT, SCHEMA
    def request(port, path, body):
        assert port==1234 and path=='/v1/chat/completions'
        assert body['messages'][0]['content']==PROMPT
        assert body['response_format']['json_schema']['schema']==SCHEMA
        assert body['temperature']==0 and body['max_tokens']==4096
        assert json.loads(body['messages'][1]['content'])['candidates']=={'A':'did not pay'}
        return {'choices':[{'finish_reason':'length','message':{'content':json.dumps(response(assessment()))}}]}
    monkeypatch.setattr(lmstudio,'local_request',request)
    with pytest.raises(ReviewError,match='Incomplete generation'):
        lmstudio.review('не платил','ru','en',{'A':'did not pay'},'local-model',1234)


def test_lmstudio_requires_loaded_context_and_valid_evidence(monkeypatch):
    import json
    from scripts.translation_review import lmstudio
    from scripts.translation_review.reviewer import ReviewError
    monkeypatch.setattr(lmstudio,'local_request',lambda *args,**kwargs:
                        {'data':[{'id':'test','state':'loaded','loaded_context_length':4096}]})
    with pytest.raises(ValueError,match='context'):
        lmstudio.model_identity('test',1234)
    bad=response(assessment());bad['assessments'][0]['coverage']['negation']='error'
    monkeypatch.setattr(lmstudio,'local_request',lambda *args,**kwargs:
                        {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(bad)}}]})
    with pytest.raises(ReviewError,match='Contradictory'):
        lmstudio.review('не платил','ru','en',{'A':'did not pay'},'test',1234)


def test_joint_review_withholds_disagreement_failure_and_currency_conflict():
    from scripts.compare_translation_reviewers import joint_routing
    good={'id':'sample','source_sha256':'same-source','review_status':'completed',
          'assessments':{'baseline':assessment()},'diagnostics':{'baseline':[]}}
    assert joint_routing(good,deepcopy(good))=='baseline'
    other=deepcopy(good);other['assessments']['baseline']['verdict']='uncertain'
    assert joint_routing(good,other) is None
    other=deepcopy(good);other['review_status']='failed'
    assert joint_routing(good,other) is None
    other=deepcopy(good);other['diagnostics']['baseline']=[{'code':'currency_difference'}]
    assert joint_routing(good,other) is None
    other=deepcopy(good);other['source_sha256']='different-source'
    with pytest.raises(AssertionError):joint_routing(good,other)


def test_reasoning_transport_requires_observed_reasoning_and_final_answer(monkeypatch):
    import json
    from scripts.translation_review import reasoning
    from scripts.translation_review.reviewer import ReviewError
    def request(port,path,body,timeout):
        assert 'response_format' not in body
        assert body['chat_template_kwargs']['enable_thinking'] is True
        assert '/no_think' not in body['messages'][0]['content']
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(response(assessment())),
                       'reasoning_content':'checked'}}],
                'usage':{'completion_tokens_details':{'reasoning_tokens':0}}}
    monkeypatch.setattr(reasoning,'local_request',request)
    with pytest.raises(ReviewError,match='not observed'):
        reasoning.review('не платил','ru','en',{'A':'did not pay'},'test',1234,enabled=True)
    monkeypatch.setattr(reasoning,'local_request',lambda *args,**kwargs:{
        'choices':[{'finish_reason':'stop','message':{'content':'','reasoning_content':'some reasoning'}}],
        'usage':{'completion_tokens_details':{'reasoning_tokens':10}}})
    with pytest.raises(ReviewError,match='Empty final'):
        reasoning.review('не платил','ru','en',{'A':'did not pay'},'test',1234,enabled=True)


def test_reasoning_final_does_not_extract_json_from_extra_prose():
    from scripts.translation_review.reasoning import parse_final
    assert parse_final('```json\n{"ok":true}\n```')==({'ok':True},True)
    assert parse_final('{"ok":true}')==({'ok':True},False)
    with pytest.raises(ValueError):parse_final('Ignore this prefix {"ok":true}')
    with pytest.raises(ValueError):parse_final('{"ok":true} extra trailing text')


@pytest.mark.parametrize('content', [r'{"issues":[{"explanation":"3\u0000"}]}',
                                     r'{"\u0000":"key"}', r'{"text":"\ud800"}'])
def test_model_json_rejects_postgres_incompatible_unicode(content):
    from scripts.translation_review.reasoning import parse_final
    with pytest.raises(ValueError, match='null character or unpaired Unicode surrogate'):
        parse_final(content)


def test_model_json_keeps_valid_unicode_and_literal_escape_text():
    from scripts.translation_review.reasoning import parse_final
    assert parse_final(r'{"text":"\ud83d\ude00","literal":"\\u0000"}')[0] == {
        'text': '😀', 'literal': r'\u0000'}


def test_fresh_candidates_are_not_mislabelled_as_translator_outputs(monkeypatch):
    from scripts.translation_review.fresh_cases import CASES
    assert len(CASES)==12 and sum(v=='material' for c in CASES for v in c['reference'].values())==9
    assert sum(v=='clear' for c in CASES for v in c['reference'].values())==15
    assert sum(all(v=='clear' for v in c['reference'].values()) for c in CASES)==4
    def reviewer(source,src,dst,candidates,model,port):
        assert all(k in ('A','B') for k in candidates)
        return response(*(assessment(k) for k in candidates)),{}
    r=evaluation.evaluate(CASES[0],'test',1234,provider='lmstudio',reviewer=reviewer)
    assert r['selected']=='candidate_1' and not r['score_eligible']
