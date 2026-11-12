"""Browse stored company leads and event briefs across search runs."""
from datetime import date

from . import bilingual, db, event_analysis

PAGE_SIZE = 25
LEAD_FIELDS = """id, company_id, run_id, subject_name, title, title_en, language, publisher,
                 published_at, (published_at AT TIME ZONE 'Europe/Moscow')::date AS publication_day,
                 url, score, report_selected, discovery_summary"""


def matching_translations(conn, query):
    """Index matching saved translations by the exact text and language used for display."""
    found = {}
    for row in conn.execute("""SELECT source,language FROM result_translations
            WHERE (russian <> source AND strpos(lower(russian),lower(%s)) > 0)
               OR (english <> source AND strpos(lower(english),lower(%s)) > 0)""", (query, query)):
        found.setdefault(row['source'], set()).add(row['language'])
    return found


def translated_lead_matches(row, found):
    if row['title'] in found or row['title_en'] in found:
        text, language = bilingual.lead_text(row)
        if language in found.get(text, ()):
            return True
    summary = row['discovery_summary']
    return summary in found and bilingual.summary_language(summary, row['language']) in found[summary]


def search(company, view, start, end, query, accepted, page):
    offset = (page - 1) * PAGE_SIZE if page is not None else 0
    with db.connect() as conn:
        translated = matching_translations(conn, query) if query else {}
        if view == 'leads':
            clauses = ['company_id=%s']
            params = [company['id']]
            for day, comparison in ((start, '>='), (end, '<=')):
                if day:
                    clauses.append("(published_at AT TIME ZONE 'Europe/Moscow')::date " + comparison + " %s")
                    params.append(day)
            if accepted:
                clauses.append('report_selected')
            if query:
                text_filter = "strpos(lower(concat_ws(' ',title,title_en,publisher,discovery_summary)),lower(%s))>0"
                params.append(query)
                if translated:
                    sources = list(translated)
                    candidates = conn.execute("""SELECT id,title,title_en,language,discovery_summary
                        FROM search_results WHERE company_id=%s
                        AND (title=ANY(%s) OR title_en=ANY(%s) OR discovery_summary=ANY(%s))""",
                        (company['id'], sources, sources, sources))
                    text_filter += " OR id=ANY(%s)"
                    params.append([r['id'] for r in candidates if translated_lead_matches(r, translated)])
                clauses.append('(' + text_filter + ')')
            where = ' FROM search_results WHERE ' + ' AND '.join(clauses)
            total = conn.execute('SELECT count(*) AS n' + where, params).fetchone()['n']
            rows = conn.execute('SELECT ' + LEAD_FIELDS + where +
                                ' ORDER BY published_at DESC NULLS LAST,id DESC LIMIT %s OFFSET %s',
                                [*params, PAGE_SIZE if page is not None else None, offset]).fetchall()
            db.mark_article_copies(conn, rows)
            return rows, total

        # Group first: a dated decision may have evidence published outside the requested range.
        members = conn.execute('SELECT ' + LEAD_FIELDS + """, event_draft, event_group, source_kind,
            exposure_paths, score_included, score_status, score_updated_at, created_at, score_evidence,
            jsonb_build_object('status',verification->>'status','source_url',verification->>'source_url',
                               'document_hash',verification->>'document_hash') AS verification
            FROM search_results WHERE company_id=%s AND event_draft ? 'event'
            ORDER BY run_id DESC,id DESC""", (company['id'],)).fetchall()
        events = event_analysis.run_events({'subjects': [company]}, members)
        matches = []
        needle = query.casefold()
        for event in events:
            day, leads = event['day'], event['sources']
            if (start and (not day or day < start)) or (end and (not day or day > end)):
                continue
            if accepted and not any(r['report_selected'] for r in leads):
                continue
            if needle and not (needle in event['title'].casefold() or needle in (event['summary'] or '').casefold()
                               or 'en' in translated.get(event['title'], ())
                               or 'en' in translated.get(event['summary'], ())
                               or any(translated_lead_matches(r, translated) for r in leads)
                               or any(needle in (r[k] or '').casefold() for r in leads
                                      for k in ('title','title_en','publisher','discovery_summary'))):
                continue
            matches.append(event)
        matches.sort(key=lambda e: e['day'] or date.min, reverse=True)
        rows = matches[offset:offset + PAGE_SIZE] if page is not None else matches
        db.mark_article_copies(conn, [r for event in rows for r in event['sources']])
        return rows, len(matches)
