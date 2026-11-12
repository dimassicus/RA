from datetime import datetime, timezone
import pytest
from ra import disclosures, google_discovery as google, idx_filings, perplexity, search

SPAN = (datetime(2026,4,1,tzinfo=timezone.utc),datetime(2026,6,30,tzinfo=timezone.utc))
ISSUER = dict(name='Acme Chemical',country_registration='Brazil')


@pytest.mark.parametrize('p,g',[(False,False),(True,False),(False,True),(True,True)])
def test_switches_route_company_search(monkeypatch,p,g):
    calls=[]
    monkeypatch.setattr(disclosures,'collect',lambda *a,**kw: ([],[]))
    monkeypatch.setattr(idx_filings,'collect',lambda *a,**kw: ([],[]))
    monkeypatch.setattr(search.news_apis,'configured_providers',lambda: [])
    monkeypatch.setattr(search,'planned_feeds',lambda *a: [])
    monkeypatch.setattr(perplexity.recovery_providers,'configured',lambda name:True)
    monkeypatch.setattr(perplexity,'collect',lambda *a,**kw: (calls.append('P') or [],[]))
    monkeypatch.setattr(google,'collect',lambda *a,primary,**kw: (calls.append('G primary' if primary else 'G gaps') or [],[]))
    search.search_company(ISSUER,provider_window=SPAN,settings=dict(use_perplexity=p,use_google=g))
    assert calls == (['P'] if p else []) + (['G gaps' if p else 'G primary'] if g else [])


def test_google_requires_single_grounding_citation_in_same_line(monkeypatch):
    line='2026-05-02 | Acme Chemical bénéficios | Acme Chemical net profit fell 4%.'
    previous='Préambule\n'
    lo=len(previous.encode())
    annotation=dict(type='url_citation',url='https://issuer.test/result',start_index=lo,end_index=lo+len(line.encode()))
    def rows(annotations):
        data={'steps':[dict(type='model_output',content=[dict(type='text',text=previous+line,annotations=annotations)])]}
        return list(google.cited_rows(data,ISSUER,SPAN,'Q1'))
    monkeypatch.setattr(google,'publisher_url',lambda url:url)
    assert rows([annotation])[0]['url'] == annotation['url']
    assert not rows([]) and not rows([annotation|dict(start_index=0)])
    assert not rows([annotation,annotation|dict(url='https://other.test')])


def test_google_targets_missing_statements_conflicting_figures_or_unresolved_order():
    def plan(rows):
        return [q for _,q,_ in google.searches(rows,[{'status':'Index checked'}],SPAN,False,[])]
    fiscal=dict(title='Acme Chemical financial results Q1 2026',published_at=SPAN[0],summary='Net profit fell 4%.')
    assert not plan([fiscal])
    assert 'financial results' in plan([fiscal|dict(title='New plant')])[0]
    assert 'conflicting' in plan([fiscal,fiscal|dict(summary='Net profit rose 9%.')])[0]
    assert 'operative ruling' in plan([fiscal,dict(title='Isi Penetapan Pengadilan dan Permohonan Keberatan',
                                                    published_at=SPAN[1],url='https://issuer.test/notice')])[0]


def test_focus_changes_discovery_queries_and_preserves_favourable_finds(monkeypatch):
    queries = []
    positive = dict(title='Acme Chemical returns to profit', url='https://issuer.test/profit',
                    published_at=SPAN[0], summary='Acme Chemical earnings increased.')
    monkeypatch.setattr(perplexity.recovery_providers, 'configured', lambda _: True)
    monkeypatch.setattr(perplexity, 'search', lambda q, s, lo, hi: queries.append((q, lo, hi)) or [positive.copy()])
    found, _ = perplexity.collect(ISSUER, SPAN, adverse_focus=True)
    assert found == [positive] and any('volatility inquiry' in q for q, _, _ in queries)
    queries.clear()
    monkeypatch.setattr(google, 'lookup', lambda s, span, q: queries.append((q, *span)) or [positive.copy()])
    google.collect(ISSUER, SPAN, [positive], [], primary=False, provider_gaps=[], adverse_focus=True)
    assert any('corruption investigations' in q for q, _, _ in queries)
    assert any('material neutral filings' in q for q, _, _ in queries)
    assert min(lo for _, lo, _ in queries) == SPAN[0] and max(hi for _, _, hi in queries) == SPAN[1]
