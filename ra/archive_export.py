"""Excel download for a filtered archive result."""
from flask import url_for
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .company_exports import result_buffer
from .i18n import headline, message, summary


def workbook(company, view, results):
    book = Workbook()
    book.properties.title = f"{company['name']} · {message('Archive search')}"
    book.remove(book.active)

    def sheet(name, columns):
        ws = book.create_sheet(message(name))
        ws.append([message(header) for header, _ in columns])
        for col, (_, width) in enumerate(columns, 1):
            cell = ws.cell(1, col)
            cell.fill = PatternFill('solid', fgColor='0F6F69')
            cell.font = Font(color='FFFFFF', bold=True)
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = 'A2'
        return ws

    def write_row(ws, line, values):
        for index, value in enumerate(values, 1):
            cell = ws.cell(line, index, ILLEGAL_CHARACTERS_RE.sub('', value) if isinstance(value, str) else value)
            if isinstance(value, str):
                cell.data_type = 's'
            cell.alignment = Alignment(vertical='top', wrap_text=True)

    events = view == 'events'
    event_columns = [('Event group', 20)] if events else []
    if events:
        briefs = sheet('Company events', event_columns + [('Company', 32), ('Date', 14), ('Date basis', 18),
                      ('Event title', 64), ('Summary', 90), ('Source leads', 16)])
    leads = sheet('Source leads' if events else 'Individual leads', event_columns + [
        ('Lead ID', 12), ('Company', 32), ('Published', 14), ('Headline', 70), ('Summary', 75),
        ('Publisher', 25), ('Score', 12), ('Report selection', 18), ('Run ID', 12),
        ('Source URL', 50), ('Representative lead', 22), ('Representative counted', 24)])
    groups = [(event, event['sources']) for event in results] if events else [(None, results)]
    lead_line = 1
    for event_line, (event, sources) in enumerate(groups, 2):
        prefix = [event['key']] if event else []
        if event:
            write_row(briefs, event_line, prefix + [event['company'], event['day'], message(event['date_label']),
                   message(event['title']), message(event['summary'] or ''), len(sources)])
        for lead in sources:
            rep = lead.get('copy_lead')
            lead_line += 1
            write_row(leads, lead_line, prefix + [lead['id'], company.get('short_name') or company['name'],
                       lead['publication_day'], headline(lead), summary(lead), lead['publisher'], lead['score'],
                       message('Accepted' if lead['report_selected'] else 'Unselected'), lead['run_id'],
                       lead['url'], rep, (lead['copy_counted'] if rep else None)])
            links = {4: url_for('article_score', result_id=lead['id'], _anchor='source-evidence', _external=True),
                     9: url_for('run_detail', run_id=lead['run_id'], _external=True)}
            if lead['url'].startswith(('https://', 'http://')):
                links[10] = lead['url']
            if rep:
                links[11] = url_for('article_score', result_id=rep, _external=True)
            for index, target in links.items():
                cell = leads.cell(lead_line, index + len(prefix))
                cell.hyperlink = target
                cell.font = Font(color='056D84', underline='single')
    for ws in book:
        ws.auto_filter.ref = ws.dimensions
    return result_buffer(book.save)
