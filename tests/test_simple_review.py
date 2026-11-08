"""Safety boundaries for simplified evaluation; no app/model/network access."""
from copy import deepcopy
import json
import pytest
from scripts.translation_review.evidence import resolve_spans,canonical
from scripts.translation_review.simple import validate,SYSTEM
from scripts.translation_review import simple
from scripts.translation_review.reviewer import ReviewError
from scripts.evaluate_translation_review import load_cases,evaluate
from scripts.translation_review.fresh_cases import CASES


def response():
    return {'reviews':[{'candidate':'A','verdict':'material_error','issues':[
        {'category':'negation','source':['did not pay'],'translation':['paid'],
         'explanation':'Negation was removed.'}]}],'correction':''}


def test_entity_unicode_whitespace_offsets():
    original="The court &apos; s ruling: Cafe\u0301\u00a0\n  did not pay EUR 50."
    spans=resolve_spans(original,["court ' s ruling",'Café did not pay EUR 50'])
    assert all(original[s['start']:s['end']]==s['text'] for s in spans)
    assert spans[0]['text']=="court &apos; s ruling"
    assert all(s['normalized_match'] for s in spans)
    assert resolve_spans('‘Volna’ said “no”.',["'Volna'",'"no"'])


@pytest.mark.parametrize('text,quote',[
    ('did not pay EUR 50','did pay EUR 50'),('EUR 50','EUR 500'),
    ('EUR 50','USD 50'),('unpaid','paid'),('Debt remains','debt remains'),
    ('first fact. second fact.','first fact...second fact'),
    ('3.5 million','35 million'),('text','&nbsp;'),
    ('court &amp;apos; order',"court ' order"),
])
def test_no_fuzzy_or_semantic_evidence_repair(text,quote):
    with pytest.raises(ValueError):resolve_spans(text,[quote])


def test_separate_fragments_and_exact_ellipsis():
    text='First fact. Intervening sentence. Second fact.'
    assert len(resolve_spans(text,['First fact','Second fact']))==2
    with pytest.raises(ValueError):resolve_spans(text,['First fact...Second fact'])
    assert resolve_spans('Literal ... in source',['...'])
    assert len(resolve_spans(text,['First fact','First fact'])) == 1
    assert canonical('&notit;')[0]=='&notit;'


@pytest.mark.parametrize('text,fragment,complete', [
    ('شهادات البنك الأهلي وبنك مصر', 'بنك مصر', 'وبنك مصر'),
    ('نشكر مصر وفخامة الرئيس السيسي', 'فخامة الرئيس السيسي', 'وفخامة الرئيس السيسي'),
    ('والطن يتجاوز 4100 دولار', 'الطن يتجاوز 4100 دولار', 'والطن يتجاوز 4100 دولار'),
    ('unpaid', 'paid', 'unpaid'),
])
def test_partial_word_evidence_gets_actionable_retry_without_relaxing_gate(text, fragment, complete):
    with pytest.raises(ValueError, match='complete words, including attached prefixes'):
        resolve_spans(text, [fragment])
    span, = resolve_spans(text, [complete])
    assert span['text'] == complete == text[span['start']:span['end']]
    assert 'Quote complete words, including attached prefixes.' in SYSTEM


def test_later_whole_word_match_still_wins_over_partial_match():
    span, = resolve_spans('unpaid then paid', ['paid'])
    assert span['start'] == 12 and span['text'] == 'paid'


def test_material_evidence_adapter_and_omission():
    data,audit=validate(response(),'They did not pay.',{'A':'They paid.'})
    assert data['assessments'][0]['verdict']=='material_error'
    assert 'coverage' not in data['assessments'][0]
    assert audit['A'][0]['source'][0]['text']=='did not pay'
    r=response();issue=r['reviews'][0]['issues'][0]
    issue.update(category='completeness',translation=[])
    assert validate(r,'They did not pay.',{'A':'They.'})
    issue['category']='negation'
    with pytest.raises(ValueError):validate(r,'They did not pay.',{'A':'They.'})


@pytest.mark.parametrize('bad',['missing','duplicate','contradictory','unsupported',
                                'invented','unknown','invalid_correction'])
def test_invalid_response_stays_withheld(bad):
    r=response()
    if bad=='missing':r['reviews']=[]
    if bad=='duplicate':r['reviews']*=2
    if bad=='contradictory':r['reviews'][0]['verdict']='faithful'
    if bad=='unsupported':r['reviews'][0]['issues']=[]
    if bad=='invented':r['reviews'][0]['issues'][0]['source']=['invented fact']
    if bad=='unknown':r['reviews'][0]['candidate']='Z'
    if bad=='invalid_correction':r['correction']={'text':'new facts'}
    with pytest.raises(ValueError):validate(r,'They did not pay.',{'A':'They paid.'})


def test_reasoning_modes_and_separate_final(monkeypatch):
    raw={'reviews':[{'candidate':'A','verdict':'faithful','issues':[]}],'correction':''}
    envelope={'model':'fake','choices':[{'finish_reason':'stop','message':{
        'content':json.dumps(raw),'reasoning_content':'private test placeholder'}}],
        'usage':{'completion_tokens_details':{'reasoning_tokens':3}}}
    requests=[]
    def local(port,path,request,timeout):requests.append(request);return deepcopy(envelope)
    monkeypatch.setattr(simple,'local_request',local)
    _,meta=simple.review('source','en','en',{'A':'source'},'fake',1234,enabled=True)
    assert meta['reasoning_enabled'] and requests[0]['max_tokens']==8192
    assert requests[0]['reasoning_effort']=='medium' and 'response_format' not in requests[0]
    with pytest.raises(ReviewError):simple.review('source','en','en',{'A':'source'},'fake',1234,enabled=False)
    envelope['choices'][0]['message']['reasoning_content']=''
    envelope['usage']['completion_tokens_details']['reasoning_tokens']=0
    with pytest.raises(ReviewError):simple.review('source','en','en',{'A':'source'},'fake',1234,enabled=True)
    assert simple.review('source','en','en',{'A':'source'},'fake',1234,enabled=False)
    envelope['choices'][0]['message']['content']=''
    with pytest.raises(ReviewError):simple.review('source','en','en',{'A':'source'},'fake',1234,enabled=False)


def test_frozen_corpus_fits_and_blinding_preserved():
    cases=load_cases()+CASES
    assert len(cases)==46 and sum(len(c['candidates']) for c in cases)==105
    for c in cases:
        content=json.dumps({'source_language':c['source_lang'],'target_language':c['target_lang'],
                            'source':c['source'],'candidates':c['candidates']},ensure_ascii=False)
        assert len((content+SYSTEM).encode())<=11500 and len(c['source'])<=4000
    c=deepcopy(CASES[0]);seen=[]
    def reviewer(source,sl,tl,candidates,m,p):
        assert set(candidates)=={'A','B'}
        seen.append(source)
        raw={'reviews':[{'candidate':n,'verdict':'faithful','issues':[]} for n in candidates], 'correction':''}
        data,_=validate(raw,source,candidates);return data,{}
    result=evaluate(c,'fake',1234,reviewer=reviewer)
    assert seen==[c['source']] and result['score_eligible'] is False

@pytest.mark.parametrize('fields', [{}, {'correction':'unsupported replacement'}])
def test_optional_correction_cannot_override_valid_reviews(fields):
    raw = {'reviews':[{'candidate':'A','verdict':'uncertain','issues':[]}], **fields}
    review, _ = validate(raw, 'original', {'A':'candidate'})
    assert review['corrected_translation'] == ''
    assert review['assessments'][0]['verdict'] == 'uncertain'


def test_shadow_review_requests_one_result_and_still_rejects_invented_evidence(monkeypatch):
    raw = response()
    raw.pop('correction')
    raw['reviews'][0]['issues'][0]['source'] = ['invented source text']
    def model(port, path, request, timeout):
        assert request['max_tokens'] == 2048 and timeout == 120
        shape = request['response_format']['json_schema']['schema']['properties']['reviews']
        assert shape['minItems'] == shape['maxItems'] == 1
        assert request['reasoning_effort'] == 'none'
        return {'model':'local', 'choices':[{'finish_reason':'stop', 'message':{'content':json.dumps(raw)}}]}
    monkeypatch.setattr(simple, 'local_request', model)
    with pytest.raises(ReviewError, match="Evidence"):
        simple.review("They did not pay.", "en", "en", {"A":"They paid."}, "local", 1234, enabled=False, shadow_only=True)
