from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import json

import pytest
from reportlab.pdfgen import canvas

from ra.article_content import canonical_url, extract, public_target
from ra.events import assign_groups, same_event
from ra.scoring import aggregate_week, score_article
from ra.verification import verify

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
SUBJECT = {'name': 'Acme Manufacturing', 'aliases': 'ACM', 'country_registration': 'Brazil'}
ARTICLE = {'id': 1, 'company_id': 1, 'run_id': 1, 'url': 'https://example.org/news',
           'title': 'Acme Manufacturing news', 'language': 'en', 'published_at': NOW, 'created_at': NOW}


def document(body, **extra):
    return {'status': 'available', 'body': body, 'language': 'en', 'method': 'fixture',
            'sha256': hashlib.sha256(body.encode()).hexdigest(), **extra}


@pytest.mark.parametrize('sentence,score', [
    ('Acme Manufacturing has filed for bankruptcy protection after missing bond payments.', -3),
    ('Acme Manufacturing announces plant closure affecting 300 workers at its Brazilian facility.', -2),
    ('Acme Manufacturing reports net losses of 40 million dollars during the third quarter.', -1),
    ('Acme Manufacturing declared a net loss of 40 million dollars.', -1),
    ('Acme Manufacturing company reported a net loss of 40 million dollars.', -1),
    ('Acme Manufacturing announced that it suffered a loss of 40 million dollars.', -1),
    ('Acme Manufacturing reduces losses following a recovery in its core market.', 1),
    ('Acme Manufacturing returns to profit following a recovery in its core market.', 2),
    ('Acme Manufacturing emerges from bankruptcy after completing its court-approved debt restructuring.', -2),
    ('Acme Manufacturing emerges from bankruptcy after completing the court-approved repayment agreement.', 3),
    ('Acme Manufacturing pede recuperação judicial.', -2),
    ('Acme Manufacturing teve um prejuízo líquido de R$ 13 milhões.', -1),
    ('A Acme Manufacturing teve um prejuízo líquido de R$ 13 milhões.', -1),
    ('A Acme Manufacturing anunciou uma recuperação judicial.', -2),
])
def test_source_attribution_and_direction(sentence, score):
    result = verify(document(sentence), SUBJECT, ARTICLE)
    # Additional independent event wording requires explicit attribution too.
    if 'debt restructuring' in sentence:
        assert result['status'] == 'needs_review'
        return
    assert result['status'] == 'supported'
    assert result['score'] == score
    assert result['evidence'][0]['sentence'] == sentence
    assessed = score_article({**ARTICLE, 'verification': result}, SUBJECT, NOW)
    assert assessed['score_included']
    assert assessed['score'] == score


@pytest.mark.parametrize('body', [
    'Acme Manufacturing reports net\nlosses of 40 million dollars.',
    'Acme Manufacturing S.A. reports net losses of 40 million dollars.',
])
def test_pdf_soft_wraps_and_legal_suffix_do_not_split_company_from_event(body):
    result = verify(document(body, method='PDF text (no OCR)'), SUBJECT, ARTICLE)
    assert result['status'] == 'supported' and result['score'] == -1
    assert result['evidence'][0]['sentence'] == body


@pytest.mark.parametrize('body', [
    "Acme Manufacturing's supplier reports net\nlosses of 40 million dollars.",
    'Acme Manufacturing reports net\nlosses of 40 million dollars. The company denied the report.',
    'Acme Manufacturing reports net\nlosses of 40 million dollars. This happened in 2020.',
    'Acme Manufacturing publishes an update.\n\nRival Holdings reports net losses of 40 million dollars.',
])
def test_pdf_soft_wrap_fix_preserves_entity_and_context_exclusions(body):
    assert verify(document(body, method='PDF text (no OCR)'), SUBJECT, ARTICLE)['status'] != 'supported'


def test_corrupt_pdf_text_is_rejected_before_cleaning_and_for_cached_documents(monkeypatch):
    import pypdf
    from types import SimpleNamespace
    body = 'Acme Manufacturing reports net losses of 40 million dollars. ' + '\x01\x02\ufffd' * 100
    monkeypatch.setattr(pypdf, 'PdfReader', lambda _: SimpleNamespace(
        is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: body)]))
    with pytest.raises(ValueError, match='corrupt character encoding'):
        extract(b'%PDF-test', 'application/pdf')
    result = verify(document(body, method='PDF text (no OCR)'), SUBJECT, ARTICLE)
    assert result['status'] == 'unavailable' and not result['evidence']


ISSUER_DECISION = (
    'A Acme Manufacturing S.A. (“Companhia”) comunica aos seus acionistas e ao mercado em geral que, '
    'no âmbito dos trabalhos de otimização de resultados das diversas plantas e ativos da Companhia, '
    'a Diretoria da Companhia decidiu (I) dar seguimento com a retomada da operação da unidade de Paranaguá (“Paranaguá”); '
    'e (II) hibernar as plantas localizadas em Ourinhos, Iguatama e Rio Verde no Brasil.'
)


def test_official_declared_issuer_owns_enumerated_board_decision():
    result = verify(document(ISSUER_DECISION, language='pt', method='PDF text (no OCR)'), SUBJECT,
                    {**ARTICLE, 'source_kind': 'official'})
    assert result['status'] == 'supported' and result['score'] == -2
    evidence = result['evidence'][0]
    assert evidence['phrase'] == 'hibernar'
    assert evidence['attribution']['company_declaration'] in ISSUER_DECISION
    assert evidence['attribution']['decision'] in ISSUER_DECISION
    assert 'retomada' in evidence['attribution']['decision']
    assert 'hibernar as plantas' in evidence['attribution']['decision']


@pytest.mark.parametrize('body', [
    ISSUER_DECISION.replace('decidiu', 'não decidiu'),
    ISSUER_DECISION.replace('hibernar', 'não hibernar'),
    ISSUER_DECISION.replace('decidiu', 'decidiu em 2020'),
    ISSUER_DECISION.replace('A Acme Manufacturing S.A.', 'A Outra Empresa S.A.'),
    ISSUER_DECISION.replace('a Diretoria da Companhia', 'o fornecedor da Companhia'),
    ISSUER_DECISION.replace('hibernar as plantas', 'pedir que Outra Empresa hiberne as plantas'),
    ISSUER_DECISION.replace('hibernar as plantas localizadas em', 'hibernar as plantas de Outra Empresa em'),
    '“' + ISSUER_DECISION + '”',
    ISSUER_DECISION.replace('a Diretoria da Companhia', 'reproduz a declaração de Outra Empresa: “a Diretoria da Companhia') + '”',
    ISSUER_DECISION.replace('(I) dar seguimento', '(I) reproduzir a declaração de Outra Empresa: “dar seguimento') + '”',
    ISSUER_DECISION.replace('(I) dar seguimento', '(I) reproduzir a declaração de Outra Empresa: "dar seguimento') + '"',
])
def test_declared_issuer_rule_rejects_denial_history_and_other_actors(body):
    result = verify(document(body, language='pt', method='PDF text (no OCR)'), SUBJECT,
                    {**ARTICLE, 'source_kind': 'official'})
    assert result['status'] != 'supported'


def test_declared_issuer_rule_requires_official_original_portuguese_pdf():
    for language, method, kind in [('pt', 'HTML article/main text', 'official'),
                                    ('en', 'PDF text (no OCR)', 'official'),
                                    ('pt', 'PDF text (no OCR)', 'news')]:
        result = verify(document(ISSUER_DECISION, language=language, method=method), SUBJECT,
                        {**ARTICLE, 'source_kind': kind})
        assert result['status'] != 'supported'


def test_portuguese_no_is_a_preposition_but_english_no_still_negates(monkeypatch):
    body = 'Acme Manufacturing anunciou fechamento no Brasil.'
    assert verify(document(body, language='pt'), SUBJECT, ARTICLE)['status'] == 'supported'
    assert verify(document(body.replace('anunciou', 'não anunciou'), language='pt'), SUBJECT, ARTICLE)['status'] != 'supported'
    assert verify(document('Acme Manufacturing has not filed for bankruptcy.', language='pt'), SUBJECT, ARTICLE)['status'] != 'supported'
    monkeypatch.setattr('ra.article_translation.checked_text', lambda _: 'Acme Manufacturing reports no net losses.')
    assert verify(document('Texto original.', language='es'), SUBJECT, ARTICLE)['status'] != 'supported'


@pytest.mark.parametrize('body', [
    "Acme Manufacturing's supplier has filed for bankruptcy.",
    'A supplier of Acme Manufacturing has filed for bankruptcy.',
    'Acme Manufacturing says Rival Holdings files for bankruptcy.',
    'Acme Manufacturing announced that Rival Holdings suffered a loss of 40 million dollars.',
    'Acme Manufacturing denied reports that it suffered a loss of 40 million dollars.',
    'Acme Manufacturing sponsors a football team. Rival Holdings files for bankruptcy.',
    'Acme Manufacturing denies bankruptcy reports.',
    'Acme Manufacturing may file for bankruptcy.',
    'Acme Manufacturing filed for bankruptcy in 2020.',
    'Acme Manufacturing reports no net losses.',
    'Acme Manufacturing announces default font settings.',
    'Acme Manufacturing reports losses in the football tournament.',
    'Acme Manufacturing expects to return to profit.',
    'ACM files for bankruptcy.',
    'Acme Manufacturing updates its website.',
    'A Acme Manufacturing informou que Outra Empresa teve um prejuízo líquido de R$ 13 milhões.',
    'A Acme Manufacturing negou um prejuízo líquido de R$ 13 milhões.',
    'A Acme Manufacturing teve um prejuízo líquido em 2020.',
    'A Acme Manufacturing anunciou uma possível recuperação judicial.',
])
def test_misattribution_and_uncertainty_do_not_enter_weekly_scores(body):
    result = verify(document(body), SUBJECT, ARTICLE)
    assert result['status'] != 'supported'
    assert not score_article({**ARTICLE, 'verification': result}, SUBJECT, NOW)['score_included']


def test_foreign_body_is_not_verified_using_english_headline_and_no_official_bypass():
    assert verify(document('Acme Manufacturing files for bankruptcy.', language='vi'), SUBJECT,
                  {**ARTICLE, 'title_en': 'Acme Manufacturing files for bankruptcy'})['status'] == 'needs_review'
    assert verify(document('Other Company files for bankruptcy.'), SUBJECT,
                  {**ARTICLE, 'source_kind': 'official'})['status'] == 'unmatched'
    assert verify(document('Acme Manufacturing files for bankruptcy.'), SUBJECT,
                  {**ARTICLE, 'discovery_service': 'FCA Warning RSS'})['status'] == 'needs_review'
    assert verify({'status': 'unavailable', 'reason': 'Paywall'}, SUBJECT, ARTICLE)['status'] == 'unavailable'


def test_html_and_pdf_extraction_exclude_navigation_and_gates():
    body = 'Acme Manufacturing has filed for bankruptcy protection after missing bond payments. ' + (
        'The board provided details of the financial position and the proceedings in a notice to investors. ' * 3)
    html = f'<nav>Other Company bankruptcy</nav><article><p>{body}</p><aside>Acme Manufacturing fraud</aside></article>'
    text, method = extract(html.encode(), 'text/html; charset=utf-8')
    assert body.strip() in text
    assert 'Other Company' not in text and 'fraud' not in text
    structured = json.dumps({'@type': 'NewsArticle', 'articleBody': body})
    assert extract(f'<script type="application/ld+json">{structured}</script>'.encode(), 'text/html')[0] == body.strip()
    with pytest.raises(ValueError, match='restricted'):
        extract(json_html({'@type': 'NewsArticle', 'articleBody': body, 'isAccessibleForFree': False}), 'text/html')
    with pytest.raises(ValueError, match='gate'):
        extract(f'<article>{body} Subscribe to continue reading</article>'.encode(), 'text/html')
    with pytest.raises(ValueError, match='Too little'):
        extract(b'<article>Brief preview</article>', 'text/html')
    output = BytesIO()
    pdf = canvas.Canvas(output)
    text_object = pdf.beginText(40, 750)
    for sentence in body.split('. '):
        text_object.textLine(sentence)
    pdf.drawText(text_object)
    pdf.save()
    text, method = extract(output.getvalue(), 'application/pdf')
    assert 'filed for bankruptcy' in text and method == 'PDF text (no OCR)'


def json_html(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'.encode()


def test_structured_article_html_preserves_paragraphs_without_markup_or_related_text():
    sentence = 'Acme Manufacturing reports net losses of 40 million dollars.'
    body = '<p>' + sentence + '</p><p>' + ('The financial statement describes operating expenses. ' * 4) + '</p>'
    body += '<aside>Acme Manufacturing files for bankruptcy.</aside>'
    text, method = extract(json_html({'@type': 'NewsArticle', 'articleBody': body}), 'text/html')
    assert method == 'JSON-LD articleBody'
    assert text.startswith(sentence + '\n')
    assert '<p>' not in text and 'bankruptcy' not in text
    result = verify(document(text), SUBJECT, ARTICLE)
    assert result['status'] == 'supported' and result['score'] == -1


@pytest.mark.parametrize('url', ['http://example.org/a', 'https://user:secret@example.org/a',
                                 'https://example.org:8443/a', 'file:///etc/passwd'])
def test_fetch_rejects_nonpublic_schemes_and_credentials(url):
    with pytest.raises(ValueError):
        public_target(url, resolver=lambda *a, **kw: [])


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', '192.168.1.1', '::ffff:127.0.0.1'])
def test_fetch_rejects_private_and_mixed_dns(address):
    with pytest.raises(ValueError, match='non-public'):
        public_target('https://example.org/a', resolver=lambda *a, **kw: [(2,1,6,'',('8.8.8.8',443)), (2,1,6,'',(address,443))])


def event(identifier, body, **extra):
    doc = document(body, final_url=f'https://publisher{identifier}.org/article')
    article = {**ARTICLE, 'id': identifier, 'url': doc['final_url'], **extra}
    article['verification'] = verify(doc, SUBJECT, article)
    article.update(score_article(article, SUBJECT, NOW))
    return article


def test_duplicate_event_grouping_is_company_scoped_and_conservative():
    sentence = 'Acme Manufacturing reports net losses of 40 million dollars during the third quarter.'
    first, second = event(1, sentence), event(2, sentence + ' The financial statement was released today.')
    assert same_event(first, second)
    unrelated = event(3, sentence.replace('40', '80'))
    other_company = {**second, 'id': 4, 'company_id': 2}
    later = event(5, sentence + ' The financial statement was released today.', published_at=NOW+timedelta(days=10))
    groups = assign_groups([first, second, unrelated, other_company])
    assert groups[1] == groups[2]
    assert groups[3] != groups[1] and groups[4] != groups[1]
    assert not same_event(first, later)
    # Identical bodies stay one document even if rediscovered much later.
    assert same_event(second, later)
    for row in (first, second, unrelated):
        row['event_group'] = groups[row['id']]
    result = aggregate_week([first, second, unrelated])
    assert result['event_count'] == 2 and result['duplicate_count'] == 1
    assert result['mean_score'] == -1
    assert len(result['groups'][0]['article_ids']) == 2
    assert canonical_url('https://publisher.org/a?id=1&utm_source=google#x') == 'https://publisher.org/a?id=1'
    assert not same_event({**first, 'verification': {}}, {**second, 'verification': {}})


def test_conflicting_auto_scores_excluded_and_review_takes_precedence():
    first = event(1, 'Acme Manufacturing files for bankruptcy.')
    second = event(2, 'Acme Manufacturing returns to profit.')
    for row in (first, second):
        row['event_group'] = 'manual:1'
    result = aggregate_week([first, second])
    assert result['score'] is None and result['group_conflicts'] == ['manual:1']
    first.update(score_status='reviewed', score=1)
    result = aggregate_week([first, second])
    assert result['score'] == 1 and result['article_ids'] == [1]
    first['score_included'] = False
    assert aggregate_week([first, second])['score'] is None


def test_redirect_revalidates_target_and_connects_only_to_pinned_ip(monkeypatch):
    from ra import article_content
    targets, connected = [], []
    def target(url):
        targets.append(url)
        if 'internal' in url:
            raise ValueError('Source resolves to a non-public address')
        return 'public.example', '8.8.8.8'
    class Response:
        status = 302
        def getheader(self, key, default=None):
            return 'https://internal.example/private' if key == 'Location' else default
    class Connection:
        def __init__(self, host, timeout):
            assert host == 'public.example'
        def request(self, *args, **kwargs):
            self._create_connection(('public.example',443))
        def getresponse(self):
            return Response()
        def close(self):
            pass
    monkeypatch.setattr(article_content, 'public_target', target)
    monkeypatch.setattr(article_content.http.client, 'HTTPSConnection', Connection)
    monkeypatch.setattr(article_content.socket, 'create_connection', lambda addr, timeout: connected.append(addr))
    with pytest.raises(ValueError, match='non-public'):
        article_content.fetch_bytes('https://public.example/article')
    assert targets == ['https://public.example/article','https://internal.example/private']
    assert connected == [('8.8.8.8',443)]


def test_title_is_not_reused_as_source_body_and_adjacent_denial_blocks():
    body = ('Rival Holdings files for bankruptcy after a collapse in demand. '
            'Acme Manufacturing manufactures products in Brazil and issued a routine update. ' * 3)
    text, _ = extract(f'<article><h1>Acme Manufacturing files for bankruptcy</h1><p>{body}</p></article>'.encode(), 'text/html')
    assert 'Acme Manufacturing files for bankruptcy' not in text
    assert verify(document(text), SUBJECT, ARTICLE)['status'] != 'supported'
    for ending in ('The company denied the report.', 'This happened in 2020.'):
        assert verify(document('Acme Manufacturing files for bankruptcy. ' + ending), SUBJECT, ARTICLE)['status'] == 'needs_review'


def test_subprocess_reader_returns_structured_failure_without_network():
    from ra.article_content import retrieve
    result = retrieve('https://127.0.0.1/private')
    assert result['status'] == 'unavailable'
    assert result['reason'] == 'Source resolves to a non-public address'


@pytest.mark.parametrize('extra,expected', [('', 'available'),
    ('<a href="/public_folder/files_upload/other.pdf">Other filing</a>', 'unavailable')])
def test_issuer_wrapper_follows_only_one_pdf(monkeypatch, extra, expected):
    from ra import article_content
    url = 'https://dpm.vn/cong-bo-thong-tin/disclosure-1940'
    pdf = 'https://dpm.vn/public_folder/files_upload/202611/filing.pdf'
    html = ('<main><a href="/public_folder/files_upload/202611/filing.pdf">Filing</a>'
            '<iframe src="/public_folder/files_upload/202611/filing.pdf"></iframe>' + extra + '</main>')
    calls = []
    def fetch(target):
        calls.append(target)
        return (html.encode(), 'text/html', url) if target == url else (b'%PDF-test', 'application/pdf', pdf)
    monkeypatch.setattr(article_content, 'fetch_bytes', fetch)
    def extract_pdf(raw, mime, parsed):
        assert raw == b'%PDF-test' and mime == 'application/pdf' and parsed is None
        return 'PVFCCo publishes its revised business plan for 2026.', 'PDF text (no OCR)'
    monkeypatch.setattr(article_content, 'extract', extract_pdf)
    result = article_content._retrieve(url)
    assert result['status'] == expected
    if expected == 'available':
        assert calls == [url, pdf] and result['final_url'] == pdf
        assert result['recovery'] == 'issuer disclosure PDF' and not result['review_only']
    else:
        assert calls == [url] and 'ambiguous' in result['reason']


def test_near_copy_grouping_does_not_merge_different_locations():
    sentence = ('Acme Manufacturing announces plant closure at its Recife facility following a significant '
                'decline in demand from customers in the local market during the current financial quarter.')
    assert not same_event(event(1, sentence), event(2, sentence.replace('Recife', 'Salvador')))


def test_html_article_listing_is_not_a_full_article():
    body = 'Acme Manufacturing files for bankruptcy. ' * 20
    with pytest.raises(ValueError, match='Multiple articles'):
        extract(f'<main><article>{body}</article><article>{body}</article></main>'.encode(), 'text/html')


def kap_page(index=1693892, blocked=False, *, duplicate=False, truncated=False):
    body = ('<table><tr><td><div>Acme Manufacturing confirms payment of its scheduled coupon. '
            'The principal remains outstanding until the contractual maturity. '
            'The previously approved issue ceiling is unchanged. </div></td></tr></table>')
    # Exercise UTF-8 byte lengths and payloads split across streamed chunks.
    body = body.replace('Acme', 'İstanbul Acme')
    payload = {'disclosure': {'disclosureBasic': {
        'disclosureIndex': index, 'isBlocked': blocked, 'companyTitle': 'Acme Manufacturing',
        'publishDate': '2026.11.06 17:06:27', 'title': 'Coupon payment'}},
        'disclosureBody': ['$40']}
    stream = '39:' + json.dumps({'sendData': payload}) + '\n40:T' + format(len(body.encode()), 'x') + ',' + body
    if truncated:
        stream = stream[:-15]
    if duplicate:
        stream += '\n41:' + json.dumps({'sendData': payload})
    chunks = [stream[:len(stream)//2], stream[len(stream)//2:]]
    return ('<main><p>Unrelated navigation bankruptcy rumour. </p></main>' + ''.join(
        '<script>self.__next_f.push(' + json.dumps([1, chunk]) + ')</script>' for chunk in chunks)).encode()


def test_kap_reader_extracts_only_matching_public_disclosure(monkeypatch):
    from ra import article_content
    url = 'https://www.kap.org.tr/tr/Bildirim/1693892'
    monkeypatch.setattr(article_content, 'fetch_bytes', lambda target: (kap_page(), 'text/html', url))
    result = article_content._retrieve(url)
    assert result['status'] == 'available'
    assert 'İstanbul Acme Manufacturing confirms payment' in result['body']
    assert 'bankruptcy rumour' not in result['body']
    assert result['recovery'] == 'KAP embedded disclosure table'
    assert result['final_url'] == url and not result['review_only']


@pytest.mark.parametrize('options', [{'index': 1693891}, {'blocked': True}, {'duplicate': True}, {'truncated': True}])
def test_kap_reader_rejects_wrong_blocked_ambiguous_or_incomplete_filing(monkeypatch, options):
    from ra import article_content
    url = 'https://www.kap.org.tr/en/Bildirim/1693892'
    monkeypatch.setattr(article_content, 'fetch_bytes', lambda target: (kap_page(**options), 'text/html', url))
    result = article_content._retrieve(url)
    assert result['status'] == 'unavailable'
    assert 'body' not in result
