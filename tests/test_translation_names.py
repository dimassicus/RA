
import pytest
from ra import article_translation as articles, headline_review, translation, translation_names as names
from ra import event_analysis

SUBJECT = {'name': 'PT Lautan Luas Tbk', 'short_name': 'Lautan Luas', 'aliases': 'LTLS\n大海'}


def test_names_round_trip_without_altering_unrelated_words():
    text = 'PT Lautan Luas Tbk, LAUTAN LUAS and LTLS report. LTLSX 下属大海公布 ZXQNAME0ZXQ'
    masked, tokens = names.mask(text, SUBJECT)
    assert len(tokens) == 4 and 'LTLSX' in masked and 'ZXQNAME0ZXQ' in masked
    assert names.restore(masked, tokens) == text
    assert not names.preserved(text, text.replace('LAUTAN LUAS', 'Broad Ocean'), SUBJECT)
    for changed in (masked.replace(next(iter(tokens)), ''), masked + next(iter(tokens))):
        with pytest.raises(ValueError):
            names.restore(changed, tokens)


def test_fallback_rejects_name_loss_then_restores_public_translation(monkeypatch):
    monkeypatch.setattr(translation, 'argos_translate', lambda *args: ('Broad Ocean profit fell', 'unsafe'))
    monkeypatch.setattr(headline_review.online_translation, 'translate', lambda *args: 'Broad Ocean profit fell')
    monkeypatch.setattr(headline_review, 'local_request', lambda *args, **kw: {
        'model': headline_review.GEMMA, 'choices': [{'finish_reason':'stop',
        'message':{'content':'{"supported":true,"english":"Broad Ocean profit fell"}'}}]})
    monkeypatch.setattr(headline_review.online_translation, 'translate_public',
                        lambda text,*args: text.replace('Laba', 'Profit at').replace('turun','fell'))
    assert translation.translate_text('Laba Lautan Luas turun', 'id', SUBJECT) == (
        'Profit at Lautan Luas fell','Google public translation')


def test_legacy_article_review_does_not_authorize_a_changed_name(monkeypatch):
    body = 'Lautan Luas rugi.'
    part = {'source':body,'english':'Broad Ocean posted a loss.','provider':'old',
            'review':{'status':'faithful','model':headline_review.QWEN,'reasoning':'off','issues':[]}}
    doc = {'status':'available','language':'id','body':body,'translation':{
        'source_hash':articles.fingerprint(body),'version':articles.VERSION,'language':'id',
        'status':'faithful','parts':[part]}}
    with pytest.raises(ValueError, match='Company name'):
        articles.checked_text(doc, SUBJECT)
    monkeypatch.setattr(headline_review,'readiness',lambda:{'ready':True})
    monkeypatch.setattr(translation,'argos_translate',lambda *a,**kw: ('Lautan Luas posted a loss.', 'test'))
    monkeypatch.setattr(headline_review,'audit',lambda *a,**kw:part['review'])
    assert articles.checked_text(articles.prepare(doc,SUBJECT),SUBJECT) == 'Lautan Luas posted a loss.'
    assert articles.passages('Words Lautan Luas fell',limit=15,subject=SUBJECT) == ['Words ', 'Lautan Luas ', 'fell']


def test_event_brief_retries_literal_name_translation():
    row={'id':1,'title':'Lautan Luas rugi','title_en':'Broad Ocean lost money','score':-1,
         'headline_review':{'status':'faithful'}}
    def model(instruction,payload,schema):
        if 'leads' in payload:
            assert payload['leads'][0]['english']==''
            return {'assignments':[{'id':1,'event':1,'sentiment':-1,'title':'Loss'}]}
        issuer = 'Lautan Luas' if 'Rewrite' in instruction else 'Broad Ocean'
        return {'title':issuer+' loss','summary':issuer+' '+('evidence '*160)}
    draft,=event_analysis.draft_company(SUBJECT,[row],model=model)
    assert draft[1]['title']=='Lautan Luas loss'


def test_event_briefs_use_one_watchlist_name_for_distinct_heringer_events():
    subject = dict(id=1, name='FERTILIZANTES HERINGER SOCIEDADE ANÔNIMA',
                   short_name='Fertilizantes Heringer', country_registration='Brazil')
    labels = {1: 'Fertilizantes Heringer', 2: 'Fertilizantes Heringer S.A.'}
    actions = {1: 'reports a CEO resignation', 2: 'calls a shareholder meeting'}
    rows = [dict(id=i, company_id=1, subject_name=label, title=f'{label} {actions[i]}',
                 score=0, url=f'https://example.test/{i}') for i, label in labels.items()]

    def model(instruction, payload, schema):
        assert payload['company'] == subject['short_name']
        if 'leads' in payload:
            return {'assignments': [dict(id=i, event=i, sentiment=0, title=actions[i]) for i in labels]}
        i = payload['event']['event']
        return dict(title=rows[i-1]['title'], summary=f'{labels[i]} {actions[i]}. ' + 'Evidence. ' * 150)

    for group in event_analysis.draft_company(subject, rows, model=model):
        for i, draft in group.items():
            rows[i-1].update(event_group=f'event:{i}', event_draft=draft)
    events = event_analysis.run_events({'subjects': [subject]}, rows)
    assert len(events) == 2
    for event in events:
        source, = event['sources']
        i = source['id']
        assert event['company'] == subject['short_name']
        assert event['title'] == f'Fertilizantes Heringer {actions[i]}'
        assert event['summary'].startswith(event['title'] + '.')
        assert source['title'] == f'{labels[i]} {actions[i]}'


def test_invalid_rewrite_uses_attributed_headline_and_continues_next_event():
    rows = [dict(id=i, title=f'Lautan Luas disclosure {i}', score=0, publisher='Issuer',
                 published_at='2026-11-06') for i in (1, 2)]
    def model(instruction, payload, schema):
        if 'leads' in payload:
            return {'assignments': [dict(id=i, event=i, sentiment=0, title='Disclosure') for i in (1, 2)]}
        if payload['event']['event'] == 2:
            return dict(title='LTLS second disclosure', summary='LTLS ' + 'evidence ' * 151)
        if 'previous_draft' in payload:
            assert '121 words' in instruction
            return dict(title='Incorrect merger', summary='unsupported ' * 260)
        return dict(title='Incorrect merger', summary='unsupported ' * 121)
    first, second = event_analysis.draft_company(SUBJECT, rows, model=model)
    assert 'Issuer published the headline “Lautan Luas disclosure 1” on 2026-11-06' in first[1]['summary']
    assert 'unsupported' not in first[1]['summary'] and 'analyst review' in first[1]['summary']
    assert second[2]['title'] == 'Lautan Luas second disclosure'


def test_related_news_link_alone_cannot_attribute_another_issuers_merger():
    row = dict(id=5112, title='Piraeus Bank absorbs its subsidiaries', score=0,
               discovery_summary='# Piraeus Bank absorbs its subsidiaries ## Related News ### Lautan Luas merger')
    draft, = event_analysis.draft_company(SUBJECT, [row], model=lambda *_: pytest.fail('no issuer evidence'))
    assert draft == {5112: {'excluded': True, 'sentiment': 0}}
