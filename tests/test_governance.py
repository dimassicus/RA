from ra.governance_roster import read_members, registry_url
from ra.relevance import governance_cues
from ra.search import governance_searches
from ra.verification import verify
import pytest


COMPANY = {'name':'Acme Manufacturing', 'country_registration':'Vietnam', 'aliases':'Acme Metals',
           'key_owners':'Acme Holdings', 'top_management':'Jane Smith (CEO)'}


def test_dedicated_searches_keep_issuer_with_names_and_native_terms():
    searches = governance_searches(COMPANY)
    assert all('"Acme Metals"' in q for q, _ in searches)
    vi = [q for q, edition in searches if edition == 'Vietnam']
    assert any('"bổ nhiệm"' in q for q in vi) and any('"Jane Smith"' in q for q in vi)
    assert any('"cổ đông lớn"' in q for q in vi) and any('"Acme Holdings"' in q for q in vi)
    assert any('"change of control"' in q and edition == '' for q, edition in searches)


@pytest.mark.parametrize('title,lang,expected', [
    ('Bổ nhiệm Phó Tổng giám đốc Acme Metals','vi',['Leadership']),
    ('Acme Manufacturing names new CFO','en',['Leadership']),
    ('Acme Manufacturing CEO presents results','en',[]),
    ('Acme Holdings appoints new CEO','en',[]),
    ('Fund acquires a 12% stake in Acme Metals','en',['Major ownership']),
    ('Acme Metals: giao dịch của cổ đông lớn','vi',['Major ownership']),
    ('Oil prices rise - Acme Metals','en',[]),
])
def test_governance_queue_preserves_identity_and_marks_only_leads(title,lang,expected):
    assert governance_cues({'title':title, 'language':lang, 'publisher':'Acme Metals'}, COMPANY) == expected


def test_departure_alone_cannot_move_score_but_independent_credit_impact_can():
    article = {'url':'https://issuer.example/filing'}
    doc = {'status':'available','body':'Acme Manufacturing CEO resigns.', 'language':'en'}
    assert verify(doc, COMPANY, article)['status'] == 'needs_review'
    doc['body'] += ' Acme Manufacturing missed debt payments.'
    assert verify(doc, COMPANY, article)['score'] == -3


def test_incomplete_registry_observations_cannot_remove_owners():
    subject = dict(COMPANY, country_registration='Brazil', tax_id='22266175000188')
    url = registry_url(subject)
    def get(qsa):
        return read_members(subject, url, lambda _:dict(cnpj=subject['tax_id'],razao_social=subject['name'],qsa=qsa))
    members = [{'nome_socio':'JANE SMITH','qualificacao_socio':'Diretor'}]
    assert list(get(members).values()) == ['JANE SMITH · Diretor']
    for incomplete in ([], members + [{}], None):
        with pytest.raises(ValueError):get(incomplete)
    assert not registry_url(COMPANY)


def test_dated_issuer_holdings_require_reconciling_total(monkeypatch):
    from ra import governance_roster as roster
    def page(total):
        return ('<table><tr><th>Shareholders</th><th>Common Shares</th><th>Total (In %)</th></tr>'
                '<tr><td>Holding SA</td><td>1.000</td><td>100,0%</td></tr>'
                f'<tr><td>Total</td><td>{total}</td><td>100,0%</td></tr></table><p>Updated on 2023.</p>').encode()
    monkeypatch.setattr(roster,'fetch_bytes',lambda url:(page('1.000'),'text/html',url))
    label, members = roster.ownership_table(roster.HERINGER_OWNERS)
    assert '2023' in label and members['holding sa'] == 'Holding SA · 100% · 1,000 common shares'
    monkeypatch.setattr(roster,'fetch_bytes',lambda url:(page('1.005'),'text/html',url))
    with pytest.raises(ValueError,match='totals disagree'):
        roster.ownership_table(roster.HERINGER_OWNERS)
