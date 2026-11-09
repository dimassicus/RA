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


def test_country_sources_require_legal_name_country_and_identifier():
    from ra import governance_sources as sources
    for country, name, identifier, *_ in sources.SOURCES:
        subject = dict(name=name, country_registration=country, tax_id=identifier)
        assert sources.issuer_matches(subject, country, name, identifier)
        for field in ('name', 'country_registration', 'tax_id'):
            assert not sources.issuer_matches(dict(subject, **{field:'wrong'}), country, name, identifier)
    assert not any('b3.com.br' in q for q, _ in governance_searches(dict(COMPANY, country_registration='Portugal')))


def test_issuer_disclosures_wont_report_partial_or_aggregate_holdings():
    from ra import governance_sources as sources
    header = 'PetroVietnam Fertilizer and Chemicals Corporation HOSE: DPM\nKey shareholders (25/03/2026):\n'
    end = '\nI. MESSAGE TO INVESTORS'
    rows = '• Parent: 59.59%\n• Nghe An Agricultural Materials\nJSC: 6.01%\n• Foreign ownership: 5.82%'
    label, holders = sources.parse_bulletin(header + rows + end)
    assert '2026-03-25' in label and len(holders) == 2
    assert holders['nghe an agricultural materials jsc'].endswith('6.01%')
    for bad in [header + rows, header + rows + '• Unknown row' + end, header + rows + '• Extra: 95%' + end]:
        with pytest.raises(ValueError):
            sources.parse_bulletin(bad)
    lines = ['Voting Rights (>=5%)','Parent (73.17%)','Company Staff Number','Full Name (Title)','PERSON (CEO)','Results']
    assert len(sources.athens('', lines)[1]) == 2
    with pytest.raises(ValueError):
        sources.athens('', lines[:-1])
    with pytest.raises(ValueError):
        sources.cms('', ['BOARD OF DIRECTORS','New person','INVESTOR RELATIONS'])


def test_exact_tax_id_gazette_and_fresh_official_notices():
    from datetime import datetime, timezone
    from ra import governance_sources as sources
    subject = dict(name='Test MMC',country_registration='Azerbaijan',tax_id='1500091001')
    publisher,url = sources.notice_source(subject)
    page = ('<article><div class="announcement-wrapper"><h2>Test MMC</h2><h2>VÖEN: 1500091001</h2>'
            '<p>Pay dəyişikliyi.</p><span>5 Noyabr 2026 17:17</span>'
            '<a href="/news_print.php?announcement_id=123"></a></div></article>').encode()
    now = datetime(2026,11,5,19,tzinfo=timezone.utc)
    item, = sources.recent_notices(subject,publisher,url,lambda _:page,now)
    assert item['url'].endswith('announcement_id=123') and item['language'] == 'az'
    assert not sources.recent_notices(subject,publisher,url,lambda _:page,now.replace(month=12))
    assert sources.recent_notices(subject,publisher,url,lambda _:page,now.replace(month=12),
                                  window=(now.replace(day=2),now.replace(day=8)))
    assert not sources.recent_notices(subject,publisher,url,lambda _:page,now,
                                      window=(now.replace(day=2),now.replace(day=4)))
    with pytest.raises(ValueError, match='tax ID'):
        sources.recent_notices(subject,publisher,url,lambda _:page.replace(b'1500091001',b'1500091002'),now)
    with pytest.raises(ValueError):
        sources.recent_notices(subject,publisher,url,lambda _:b'<html>maintenance</html>',now)
    assert not sources.recent_notices(subject,publisher,url,lambda _:'axtarışa uyğun elanlar tapılmadı'.encode(),now)


def test_pvfcco_discovery_includes_dated_financial_reports():
    from datetime import datetime, timezone
    from ra.governance_sources import recent_notices
    url = 'https://dpm.vn/bao-cao-tai-chinh/quarterly-report'
    page = (f'<p>pvfcco.com.vn</p><a href="{url}"><span class="file-date">29/10/2026</span>'
            '<span class="file-item-name">Consolidated Q3 results</span></a>').encode()
    start, end = datetime(2026,10,26,tzinfo=timezone.utc), datetime(2026,11,1,tzinfo=timezone.utc)
    item, = recent_notices({}, 'PVFCCo investor disclosures', 'https://dpm.vn/quan-he-nha-dau-tu',
                           lambda _: page, window=(start,end))
    assert item['url'] == url and item['source_kind'] == 'official'


def test_issuer_personnel_disclosure_is_a_governance_lead_only():
    article = dict(title='Thông báo thay đổi nhân sự',language='vi',source_kind='official',
                   discovery_service='PVFCCo investor disclosures')
    assert governance_cues(article,COMPANY) == ['Leadership']
    assert not governance_cues(dict(article,verification={'status':'unmatched'}),COMPANY)


def test_partial_gazette_search_keeps_verified_notice_and_records_error():
    from datetime import datetime, timezone
    from ra.governance_sources import recent_notices
    page = ('<article><div class="announcement-wrapper"><h2>X</h2><h2>VÖEN: 1234567890</h2><p>A notice</p>'
            '<span>5 Noyabr 2026 10:00</span><a href="/news_print.php?announcement_id=1"></a></div></article>'
            '<a class="next page-numbers"></a>').encode()
    calls, errors = [], []
    def fetch(url):
        calls.append(url)
        if len(calls) == 2:
            raise TimeoutError('timeout')
        return page
    items = recent_notices({'tax_id':'1234567890'},'Vergilər official notices','https://vergiler.az/?search_type=voen',
                           fetch, datetime(2026,11,5,15,tzinfo=timezone.utc),errors)
    assert len(items) == 1 and calls[-1].endswith('&page=2') and 'timeout' in errors[0]


def test_egyptian_directory_preserves_entity_scope_and_checks_identity_marker():
    from ra.governance_sources import delemar_entity
    data = ['المسؤولين', 'المدير العام', 'مدير الشركة', 'الموبايل', 'www.delemaralu.com']
    label, members = delemar_entity('', data)
    assert 'undated' in label and len(members) == 1
    with pytest.raises(ValueError):
        delemar_entity('', data[:-1])
