from datetime import datetime, timedelta, timezone

from ra import company_developments, decision_review, event_analysis, perplexity, search

SUBJECT = {'name':'PT Lautan Luas Tbk','aliases':'LTLS','short_name':'Lautan Luas','country_registration':'Indonesia'}
DAY = datetime(2026,7,3,tzinfo=timezone.utc)
NOTICE = 'Isi Penetapan Pengadilan dan Permohonan Keberatan - Lautan Luas'


def test_company_search_reaches_perplexity_despite_other_provider_rotation(monkeypatch):
    calls = []
    def collect(subject,window,seeds,progress):
        calls.append((subject['name'],window))
        return [dict(url='https://issuer.test/doc',title='Issuer filing')], []
    monkeypatch.setattr(perplexity,'collect',collect)
    from ra import disclosures, idx_filings
    monkeypatch.setattr(disclosures,'collect',lambda *a,**k:([],[]))
    monkeypatch.setattr(idx_filings,'collect',lambda *a,**k:([],[]))
    monkeypatch.setattr(search,'planned_feeds',lambda *a:[])
    monkeypatch.setattr(search.news_apis,'configured_providers',lambda:[])
    for name in ('Acme','Example'):
        results,_=search.search_company({'name':name,'_news_providers':[]},provider_window=(DAY,DAY+timedelta(days=7)))
        assert results[0]['title']=='Issuer filing'
    assert len(calls)==2 and all(w[0]==DAY for _,w in calls)


def test_discovery_windows_and_targeted_ruling_query(monkeypatch):
    calls=[]
    monkeypatch.setattr(perplexity.recovery_providers,'configured',lambda p:True)
    def request(provider, path, *,body):
        calls.append(body)
        return {'results':[dict(title=NOTICE,url='https://issuer.test/court?utm_source=ref',date='2026-07-03',
                                snippet='Lautan Luas filing'),dict(title='Lautan Luas',url='https://issuer.test/old',
                                date='2025-04-01',last_updated='2026-07-03')]}
    monkeypatch.setattr(perplexity.recovery_providers,'request',request)
    rows,gaps=perplexity.collect(SUBJECT,(DAY-timedelta(days=10),DAY+timedelta(days=6)))
    assert not gaps and [r['url'] for r in rows]==['https://issuer.test/court']
    assert len(calls)==5 and 'Isi Penetapan' in calls[-1]['query']
    assert calls[-1]['search_before_date_filter']=='07/10/2026'


def test_inaccessible_decision_never_implies_still_pending():
    row=dict(id=1,score=0,title=NOTICE,published_at=DAY,url='https://issuer.test/ruling')
    def model(instruction,payload,schema):
        assert 'leads' in payload
        return {'assignments':[dict(id=1,event=1,sentiment=0,title='Lautan Luas ruling')]}
    draft,=event_analysis.draft_company(SUBJECT,[row],model=model)
    assert draft[1]['decision']['status']=='unconfirmed' and 'pending' not in draft[1]['summary']
    assert not company_developments.decision_notice({'title':'Lautan Luas awaits a court ruling'})


def test_only_verbatim_disposition_and_remaining_exposure_accepted():
    url='https://issuer.test/ruling'
    returned='The court ordered five assets returned.'
    seized='Two properties remain under seizure.'
    source=returned+' '+seized
    members=[dict(decision_notice=True,title=NOTICE,url=url,published='2026-07-03',sources=[
        dict(kind='original publisher text',text='intro. '*600+source+' ending.'*500,url=url)])]
    def run(update):
        def model(instruction, data, schema):
            if 'facts' in data:
                return {'supported':True}
            lines = data['source_lines']
            ids = {quote:next(i for i,v in lines.items() if quote in v['text']) for quote in (returned,seized)}
            return dict(outcome='Partial release of five assets.',outcome_line=ids[returned],
                        remaining=seized,remaining_line=ids[seized],residual_adverse=True) | update
        return decision_review.disposition(members,'Lautan Luas',model)
    assert run({})['status']=='documented'
    assert run({'outcome_line':9999})['status']=='unconfirmed'
    assert run({'remaining':'','remaining_line':0})['status']=='unconfirmed'


def test_court_date_is_checked_against_document_and_language():
    assert decision_review.mentions_day('Kamis (2/7/2026)', '2026-07-02', 'id')
    assert decision_review.mentions_day('On July 2, 2026, the court ruled.', '2026-07-02')
    assert not decision_review.mentions_day('Kamis (2/7/2026)', '2026-07-03', 'id')
    assert not decision_review.mentions_day('Published July 3, 2026.', '2026-07-02')


def test_provider_failure_keeps_successful_queries_and_reports_gap(monkeypatch):
    monkeypatch.setattr(perplexity.recovery_providers,'configured',lambda _:True)
    calls=[]
    def request(*args, **kwargs):
        calls.append(1)
        if len(calls)>1:
            raise ValueError('Perplexity HTTP 429')
        return {'results':[{'title':'Lautan Luas financing update','url':'https://issuer.test/news/1',
                            'snippet':'Lautan Luas reports earnings','date':'2026-07-03'}]}
    monkeypatch.setattr(perplexity.recovery_providers,'request',request)
    rows,errors=perplexity.collect(SUBJECT,(DAY-timedelta(days=1),DAY+timedelta(days=1)))
    assert len(rows)==1 and errors==['Perplexity HTTP 429']


def test_draft_uses_verified_followup_even_if_model_groups_coverage_separately():
    text = 'After the hearing the court ordered five assets returned. Two properties remain under seizure.'
    url = 'https://press.test/later-report'
    row = dict(id=3,title=NOTICE,published_at=DAY,url='https://issuer.test/notice',score=0,
               decision_sources=[dict(kind='original publisher text',url=url,text=text)])
    def model(instruction,payload,schema):
        if 'source_lines' in payload:
            assert payload['source_lines'][2]['url']==url
            return dict(outcome='Five assets ordered returned.',outcome_line=1,remaining='Two still seized.',
                        remaining_line=2,residual_adverse=True,decision_day='',date_line=0)
        if 'facts' in payload:
            return {'supported':True}
        if 'leads' in payload:
            return {'assignments':[dict(id=3,event=0,sentiment=1,title='Good news')]}
        return {'title':'Lautan Luas update','summary':'The original application still awaits the decision.'}
    draft,=event_analysis.draft_company(SUBJECT,[row],model=model)
    assert draft[3]['sentiment']==-1 and draft[3]['decision']['source_url']==url
    assert 'five' in draft[3]['summary'].lower() and 'awaits' not in draft[3]['summary']


def test_long_pdf_retains_operative_page_and_marks_extraction_incomplete():
    from io import BytesIO
    from reportlab.pdfgen import canvas
    from ra import article_content
    out = BytesIO()
    pdf = canvas.Canvas(out)
    for n in range(40):
        pdf.drawString(50,700,'Two investment properties remain under seizure.' if n==22 else 'Company filing.')
        pdf.showPage()
    pdf.save()
    body,method=article_content.extract(out.getvalue(),'application/pdf')
    assert 'PDF page 23' in body and 'remain under seizure' in body and 'PDF page 24' not in body
    doc=article_content.source_document(body,method,'https://issuer.test/filing.pdf')
    assert doc['review_only'] is True
