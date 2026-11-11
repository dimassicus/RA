from datetime import datetime, timezone
import json

from ra import article_translation, disclosures, idx_filings

ISSUER = dict(id=11, name='PT Example Chemicals Tbk', aliases='EXCH', country_registration='Indonesia')
WINDOW = (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 11, 11, tzinfo=timezone.utc))
PDF = 'https://www.idx.co.id/StaticData/NewsAndAnnouncement/ANNOUNCEMENTSTOCK/202604/a.pdf'


def test_exchange_checks_identity_dates_pagination_periods_and_attachments():
    calls = []
    def fetch(url):
        calls.append(url)
        if 'Profiles' in url:
            return json.dumps({'Profiles':[dict(KodeEmiten='EXCH', NamaEmiten=ISSUER['name'])]})
        offset = int('indexFrom=1' in url)
        info = dict(Id2=str(offset), Kode_Emiten='EXCH ', JenisPengumuman='STOCK',
                    NoPengumuman=str(offset), TglPengumuman='2026-04-28T21:00:00',
                    JudulPengumuman='Laporan Keuangan' if not offset else 'Volatilitas Transaksi')
        documents = [('FinancialStatement-2026-I-EXCH.pdf',PDF),('letter.pdf',PDF.replace('/a.', '/b.'))]
        return json.dumps(dict(ResultCount=2, SearchParams=dict(KodeEmiten='EXCH',DateFrom='01/01/2026 12:00:00 AM',DateTo='11/11/2026 12:00:00 AM',indexfrom=offset),
                  Replies=[dict(pengumuman=info, attachments=[dict(OriginalFilename=n, FullSavePath=u)
                                                            for n,u in documents[offset:]])]))
    coverage = []
    rows, gaps = idx_filings.collect(ISSUER,fetch,WINDOW,coverage=coverage)
    assert not gaps and len(rows) == 2 and len(calls) == 3
    assert coverage[0]['status'] == 'Index checked' and coverage[0]['periods'] == ['Q1 2026']
    assert rows[0]['filing_attachments'] == [rows[1]['url']]
    assert rows[0]['published_at'].astimezone(timezone.utc).hour == 14

    coverage = []
    rows, complete = idx_filings.exchange_archive(dict(ISSUER,name='PT Other Tbk'),'EXCH',*WINDOW,fetch,coverage)
    assert not complete and not rows and 'identity mismatch' in coverage[0]['detail']


def test_failed_later_page_keeps_earlier_entries_and_incomplete_status():
    def fetch(url):
        if 'Profiles' in url:
            return json.dumps({'Profiles':[dict(KodeEmiten='EXCH', NamaEmiten=ISSUER['name'])]})
        if 'indexFrom=1' in url:
            raise OSError('Timeout')
        return json.dumps(dict(ResultCount=2,SearchParams=dict(KodeEmiten='EXCH',DateFrom='01/01/2026 12:00:00 AM',DateTo='11/11/2026 12:00:00 AM',indexfrom=0),Replies=[
            dict(pengumuman=dict(Id2='1',Kode_Emiten='EXCH',JenisPengumuman='STOCK',NoPengumuman='1',
                 TglPengumuman='2026-03-04T17:00:00',JudulPengumuman='Notice'),
                 attachments=[dict(OriginalFilename='Notice.pdf',FullSavePath=PDF)])]))
    coverage=[]
    rows, complete = idx_filings.exchange_archive(ISSUER,'EXCH',*WINDOW,fetch,coverage)
    assert len(rows) == 1 and not complete and coverage[0]['entries_read'] == 1


def test_dated_issuer_index_distinguishes_empty_interval_from_missing_coverage():
    url = 'https://example.com/notices'
    s = ISSUER | {'disclosure_sources':dict(identity=disclosures.identity(ISSUER), sources=[dict(url=url,kind='page')])}
    body = b'<script type="application/ld+json">{"datePublished":"2026-09-01T11:00:00Z","headline":"Update","url":"/update"}</script>'
    coverage=[]
    rows,_=disclosures.collect(s,window=WINDOW,coverage=coverage,fetch=lambda _: (body,'text/html',url))
    assert len(rows) == 1 and coverage[0]['first'] == '2026-09-01' and '2026-01-01' in coverage[0]['detail']


def test_table_and_ruling_passages_retain_provenance_under_document_budget():
    pad = 'Other information. ' * 340
    table = f'PDF page 1\n{ISSUER["name"]} In millions of IDR.\n{pad}\nPDF page 18\nStatement of profit or loss: Q1 2026 / Q1 2025\n{pad}\nPDF page 20\nNet profit 37,413 39,074.\n{pad}'
    legal = f'PDF page 1\n{ISSUER["name"]}\n{pad}\nPDF page 3\nThe court ordered five assets returned.\n{pad}\nTwo assets remain under seizure.\n{pad}'
    for body, decision, phrases in [(table,False,['millions of IDR','Q1 2026 / Q1 2025','37,413 39,074']),
                                      (legal,True,['five assets returned','Two assets remain under seizure'])]:
        result = article_translation.bounded_source(dict(body='Cover note.',linked_documents=[dict(body=body)]),ISSUER,decision)
        doc, = result['linked_documents']
        assert all(phrase in doc['body'] for phrase in phrases)
        assert result['review_scope']['selected_length'] == len(doc['body']) + len('Cover note.')
        assert len(doc['body']) <= 5000
        assert doc['body'] == '\n\n[…]\n\n'.join(body[s['start']:s['end']] for s in doc['excerpt']['ranges'])
        assert all(s['pages'] for s in doc['excerpt']['ranges'])


def test_same_site_archive_pagination_preserves_earlier_page_on_failure():
    url = 'https://example.com/filings'
    issuer = ISSUER | {'disclosure_sources':dict(identity=disclosures.identity(ISSUER), sources=[dict(url=url,kind='page')])}
    def fetch(path):
        if path.endswith('3'):
            raise OSError('Archive unavailable')
        page = 2 if path.endswith('2') else 1
        record = dict(datePublished=f'2026-0{8-page}-04T17:00:00Z',headline='Notice',url=f'/doc{page}')
        return (f'<a rel="next" href="?page={page+1}">Older</a><script type="application/ld+json">{json.dumps(record)}</script>'.encode(),'text/html',path)
    coverage=[]
    rows,gaps=disclosures.collect(issuer,window=WINDOW,coverage=coverage,fetch=fetch)
    assert len(rows)==2 and gaps and coverage[0]['status']=='Incomplete' and coverage[0]['first']=='2026-06-04'

def test_attachment_lengths_are_independent_of_other_documents():
    small = dict(body='a' * 4000, translation={'status':'faithful'})
    parent = dict(body='p' * 6000, linked_documents=[small, dict(body='b' * 7000)])
    result = article_translation.bounded_source(parent, ISSUER)
    assert [len(d['body']) for d in [result, *result['linked_documents']]] == [5000,4000,5000]
    assert result['linked_documents'][0] is small
    all_short = dict(body='x' * 5000, linked_documents=[small])
    assert article_translation.bounded_source(all_short, ISSUER) is all_short
