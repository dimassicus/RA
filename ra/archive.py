"""Browse stored company leads and event briefs across search runs."""
from datetime import date

from . import db, event_analysis

PAGE_SIZE = 25
LEAD_FIELDS = """id, company_id, run_id, subject_name, title, title_en, language, publisher,
                 published_at, (published_at AT TIME ZONE 'Europe/Moscow')::date AS publication_day,
                 url, score, report_selected, discovery_summary"""


def search(company, view, start, end, query, accepted, page):
    offset = (page - 1) * PAGE_SIZE if page is not None else 0
    with db.connect() as conn:
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
                clauses.append("strpos(lower(concat_ws(' ',title,title_en,publisher,discovery_summary)),lower(%s))>0")
                params.append(query)
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
                               or any(needle in (r[k] or '').casefold() for r in leads for k in ('title','title_en','publisher'))):
                continue
            matches.append(event)
        matches.sort(key=lambda e: e['day'] or date.min, reverse=True)
        rows = matches[offset:offset + PAGE_SIZE] if page is not None else matches
        db.mark_article_copies(conn, [r for event in rows for r in event['sources']])
        return rows, len(matches)
