from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from openpyxl import load_workbook

from ra import bilingual, db, event_analysis, i18n, score_store, worker
from ra.app import create_app
from test_score_store import database


@pytest.mark.parametrize('scope', ['all', 'selected'])
def test_both_views_and_export_share_one_scored_record(database, monkeypatch, scope):
    subject = db.company_snapshot(database.execute(
        "INSERT INTO companies(name,country_registration) VALUES ('Acme Manufacturing','Brazil') RETURNING *"
    ).fetchone())
    run_id = db.create_run(scope, [subject])
    original = 'Acme Manufacturing faces bankruptcy'
    summary = 'The company has missed a payment.'
    brief = 'The company reported a missed payment to its creditors.'
    translations = {original: 'Acme Manufacturing грозит банкротство',
                    summary: 'Компания пропустила платёж.',
                    brief: 'Компания сообщила кредиторам о пропущенном платеже.'}
    calls = []
    def translator(text, subject):
        calls.append(text)
        return translations.get(text, 'Переведено для теста')
    monkeypatch.setattr(bilingual.russian, 'translate', translator)
    lead = dict(title=original, publisher='Issuer', url='https://issuer.test/report', query='Acme',
                language='en', summary=summary, published_at=datetime.now(timezone.utc)-timedelta(hours=1),
                signal_terms=[], source_kind='official')
    worker.process_run(run_id, searcher=lambda company: ([lead, lead], []),
                       roster_check=lambda *args: None, topic_searcher=lambda *args, **kwargs: [])
    article, = db.get_results(run_id)
    rid = article['id']
    monkeypatch.setattr(event_analysis, 'draft_company', lambda *args, **kwargs:
                        iter([{rid: dict(event=rid, sentiment=-1, title=original, summary=brief)}]))
    event_analysis.process(run_id)
    db.set_report_selection(run_id, [rid])
    before = score_store.weekly_history(subject['id'])
    assert sum(r['metrics']['event_count'] for r in before) == 1
    assert db.get_run(run_id)['result_count'] == 1
    # No Russian page has been opened. The run itself enqueued all the display text.
    while bilingual.process_one():
        pass
    assert all(calls.count(text) == 1 for text in translations)

    client = create_app().test_client()
    path = f'/runs/{run_id}?view=all'
    english = client.get(path).get_data(as_text=True)
    assert '<html lang="en">' in english and brief in english
    with client.session_transaction() as session:
        token = session['csrf_token']
    response = client.post('/language', data=dict(csrf_token=token, language='ru', next=path))
    assert response.location == path
    localized = client.get(path).get_data(as_text=True)
    assert '<html lang="ru">' in localized
    for text in translations.values():
        assert text in localized
    assert brief not in localized
    weeks = client.get(f"/api/scores/weekly?company_id={subject['id']}").json['weeks']
    event, = [e for week in weeks for e in week['contributions']]
    assert event['title'] == translations[original] and event['article_ids'] == [rid]
    assert localized.count(f'name="selected_ids" value="{rid}"') == 1
    book = load_workbook(BytesIO(client.get(f'/runs/{run_id}/report.xlsx').data))
    sheet = book.worksheets[1]
    assert sheet.max_row == 2 and translations[original] in [cell.value for cell in sheet[2]]
    assert score_store.weekly_history(subject['id']) == before
    assert score_store.refresh_scores(run_id)['articles_updated'] == 0
    assert client.post('/language', data=dict(language='en')).status_code == 400
    assert client.post('/language', data=dict(language='fr', csrf_token=token)).status_code == 400
    assert client.post('/language', data=dict(language='en', csrf_token=token, next='//outside.test')).location == '/'
    assert '<html lang="en">' in client.get(path).get_data(as_text=True)


def test_retry_preserves_one_job_and_russian_original(database, monkeypatch):
    text = 'Компания сообщила о падении прибыли.'
    subject = {'name': 'Acme', 'comment': 'Private notes'}
    run_id = db.create_run('one_off', [subject])
    bilingual.enqueue(database, text, 'ru', subject, run_id=run_id)
    bilingual.enqueue(database, text, 'ru', subject, run_id=run_id)
    from ra import translation
    monkeypatch.setattr(translation, 'translate_text', lambda *args: ('', 'offline'))
    assert bilingual.process_one()
    assert not bilingual.process_one()
    row, = database.execute('SELECT * FROM result_translations').fetchall()
    assert row['russian'] == text and not row['english'] and 'comment' not in row['subject']
    database.execute('UPDATE result_translations SET retry_at=now()')
    monkeypatch.setattr(translation, 'translate_text', lambda *args: ('The company reported falling profits.', 'test'))
    assert bilingual.process_one()
    assert i18n.headline({'title': text, 'language': 'ru'}, 'ru') == text
    assert i18n.headline({'title': text, 'language': 'ru'}, 'en') == 'The company reported falling profits.'
    assert not bilingual.process_one()


def test_moscow_cutoff_restores_only_eligible_work_and_archives_stay_read_only(database, monkeypatch):
    from test_score_store import seed

    _, old, old_lead = seed(database, title='Old filing')
    _, new, new_lead = seed(database, title='New filing', url='https://example.test/new')
    database.execute("UPDATE search_runs SET created_at='2026-11-12 20:59:59+00' WHERE id=%s", (old,))
    database.execute("UPDATE search_runs SET created_at='2026-11-12 21:00:00+00' WHERE id=%s", (new,))
    database.execute("UPDATE search_results SET published_at='2025-01-01', discovery_summary='Shared summary' WHERE id=%s",
                     (new_lead['id'],))
    for source in ('Old filing', 'Shared summary', 'Saved copy'):
        database.execute("""INSERT INTO result_translations(key,source,language,english,russian)
            VALUES (%s,%s,'en',%s,%s)""", (bilingual.key(source, 'en'), source, source,
                                          'Готовый перевод' if source == 'Saved copy' else ''))
    bilingual.restore_run_queue(database)
    bilingual.restore_run_queue(database)
    bilingual.enqueue(database, 'Old-only retry', run_id=old)
    assert not database.execute("SELECT 1 FROM result_translations WHERE source='Old-only retry'").fetchone()
    client = create_app().test_client()
    with client.application.test_request_context(f'/runs/{old}'):
        assert i18n.ru('Old filing') == 'Old filing'
        assert i18n.ru('Saved copy') == 'Готовый перевод'
        assert i18n.ru('Archive-only miss') == 'Archive-only miss'
    with client.application.test_request_context(f'/runs/{new}'):
        assert 'Ожидает перевода' in i18n.ru('New detail')
    assert not database.execute("SELECT 1 FROM result_translations WHERE source='Archive-only miss'").fetchone()
    calls = []
    monkeypatch.setattr(bilingual.russian, 'translate', lambda text, subject: calls.append(text) or 'Перевод')
    while bilingual.process_one():
        pass
    assert {'New filing', 'Shared summary', 'New detail'} <= set(calls)
    assert calls.count('Shared summary') == 1 and 'Old filing' not in calls
    assert not bilingual.process_one()
