
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
