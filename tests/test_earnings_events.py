from datetime import datetime, timezone

import pytest

from ra import event_analysis, scoring


ISSUER = {"name": "Acme Metals"}
QUOTE = "Net profit fell 4% in the quarter."
ROW = dict(id=5, title="Acme Metals revenue rises", score=1, source_kind="news",
           language="en", published_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
           discovery_summary="Acme Metals reported higher sales. " + QUOTE)


def model(quote):
    def reply(instruction, payload, schema):
        if "earnings_reports" in payload:
            return {"findings": [dict(id=5, passage=i) for i, line in payload["earnings_reports"][5].items() if quote and quote in line]}
        if "leads" in payload:
            return {"assignments": [dict(id=5, event=0, title="Revenue growth", sentiment=1,
                                         profit_decline_quote=quote)]}
        return {"title": "Acme Metals quarterly profit fell", "summary": ("Acme Metals quarterly profit fell. " * 35).strip()}
    return reply


def test_profit_excerpt_overrides_positive_headline_and_neutral_grouping():
    drafts, = event_analysis.draft_company(ISSUER, [ROW], model=model(QUOTE))
    draft = drafts[5]
    assert draft['event'] == 5 and draft['sentiment'] == -1 and draft['profit_decline_quote'] == QUOTE
    row = dict(ROW, event_draft=draft, verification={'status': 'unavailable', 'reason': '403'})
    score = scoring.score_article(row, ISSUER)
    assert score['score'] == -1 and not score['score_included']
    assert event_analysis.candidate(dict(row, **score))


def test_positive_source_verification_does_not_certify_a_separate_adverse_finding():
    row = dict(ROW, verification={'status': 'supported', 'score': 1, 'evidence': [], 'reason': 'Higher revenue'})
    drafts, = event_analysis.draft_company(ISSUER, [row], model=model(QUOTE))
    score = scoring.score_article(dict(row, event_draft=drafts[5]), ISSUER)
    assert score['score'] == -1 and not score['score_included'] and score['score_status'] == 'needs_review'


@pytest.mark.parametrize('quote', ['', 'A different company reported a profit decline.'])
def test_purely_positive_and_unsubstantiated_profit_declines_stay_out(quote):
    drafts, = event_analysis.draft_company(ISSUER, [dict(ROW, discovery_summary='Revenue increased.')], model=model(quote))
    assert drafts[5] == {'excluded': True, 'sentiment': 1}


@pytest.mark.parametrize('changes', [dict(score_status='reviewed'), dict(source_kind='context'),
                                    dict(verification={'status':'unmatched'}), dict(exposure_paths=['Sector'])])
def test_positive_triage_preserves_analyst_and_identity_boundaries(changes):
    assert not event_analysis.triage_candidate(dict(ROW, **changes))


def test_financial_review_reads_middle_of_source_even_if_grouper_omits_decline():
    body = ('Market background. ' * 115) + QUOTE + ' ' + ('Additional detail. ' * 110)
    row = dict(ROW, discovery_summary='', event_sources=[dict(url='https://example.test/report',
               kind='original publisher text', language='en', text=body)])
    def reply(instruction, payload, schema):
        if 'leads' in payload:
            return {'assignments': [dict(id=5, event=0, title='Higher revenue', sentiment=1, profit_decline_quote='')]}
        return model(QUOTE)(instruction, payload, schema)
    drafts, = event_analysis.draft_company(ISSUER, [row], model=reply)
    assert drafts[5]['profit_decline_quote'] == QUOTE and drafts[5]['sentiment'] == -1


@pytest.mark.parametrize('rewrite_succeeds', [False, True])
def test_higher_target_and_lower_actual_profit_stay_distinct(rewrite_succeeds):
    target = "Acme Metals raised its annual net profit target."
    row = dict(ROW, title=target, discovery_summary=QUOTE)
    good = "Acme Metals raises annual profit target as quarterly net profit falls"
    reversed_title = "Acme Metals cuts annual profit target as quarterly net profit rises"
    checked = []

    def reply(instruction, data, schema):
        if 'earnings_reports' in data:
            lines = data['earnings_reports'][5]
            return {'findings': [dict(id=5, passage=next(i for i,v in lines.items() if v == QUOTE),
                                      target_passage=next(i for i,v in lines.items() if v == target))]}
        if 'leads' in data:
            return model(QUOTE)(instruction, data, schema)
        if 'actual' in data:
            assert data['actual'] == QUOTE and data['target'] == target
            checked.append(data['title'])
            return {'consistent': data['title'] == good}
        title = good if rewrite_succeeds and 'previous_draft' in data else reversed_title
        # Include the word "decline" so the general earnings check alone cannot pass a reversal.
        return dict(title=title + (" despite earlier decline" if title != good else ""),
                    summary=(good + ". ") * 15)

    draft, = event_analysis.draft_company(ISSUER, [row], model=reply)
    assert len(checked) == 2
    assert draft[5]['profit_decline_quote'] == QUOTE and draft[5]['sentiment'] == -1
    if rewrite_succeeds:
        assert draft[5]['title'] == good
    else:
        assert QUOTE in draft[5]['summary'] and target in draft[5]['summary']
        assert reversed_title not in draft[5]['title']
