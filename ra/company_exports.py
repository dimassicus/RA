"""Company-level monitoring exports using the app's existing report engines."""
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Table, TableStyle

from .risk_factors import notes as factor_notes
from .headline_review import review_note
from .company_report import comparison_rows, driver, evidence_rows, rating_rows
from .relevance import headline
from .reports import _font_name, _safe_excel, report_translator, russian_headline
from .scoring import WEEK_TIMEZONE


INK, TEAL, MUTED, PALE = "18373B", "096B67", "496368", "EAF3F1"
BRANCHES = {"Company": "Company news", "Sector": "Sector news", "Country / region": "Country and region news"}
RATINGS = ("Period", "Cutoff", "Score", "Search coverage", "Scored events")
COVERAGE = ("Branch", "Relevant sources", "Impact supported", "Awaiting check")
BASIS = ("Scores range from −1 (negative) to +1 (positive). Monday cutoffs cover the preceding seven days in Moscow. "
         "Earlier ratings use the snapshot available at the comparison time; legacy scales are labelled. A dash means no rating.")
FLOW_NOTE = "Counts reflect coverage accumulated in each publication week. The current week may still be in progress."


BRIEF_LABELS = {
    "Company brief": "Обзор компании", "Company monitoring report": "Отчёт по мониторингу компании",
    "Company news": "Новости компании", "Sector news": "Отраслевые новости", "Country and region news": "Новости стран и регионов",
    "Sector": "Отрасль", "Country / region": "Страна / регион", "Sources": "Источники",
    "Period": "Период", "Cutoff": "Дата отсечения", "Score": "Оценка", "Search coverage": "Охват поиска", "Scored events": "Учтённые события",
    "Branch": "Направление", "Relevant sources": "Релевантные источники", "Impact supported": "Влияние подтверждено", "Awaiting check": "Ожидают проверки",
    "Latest relevant headlines": "Последние материалы по теме", "Leadership and major ownership leads": "Руководство и крупные акционеры",
    "Risk and sentiment history": "Динамика рисков и тональности", "Next dates to watch": "Предстоящие даты",
    "How newsflow changed": "Изменения в новостном потоке", "Evidence by branch": "Источники по направлениям",
    "Search coverage gaps": "Пробелы в охвате поиска", "Sources and verification notes": "Источники и результаты проверки",
    "No matching headlines in this run’s seven-day window.": "За семь дней этого поиска подходящих материалов не найдено.",
    "No matching headlines in the seven-day window.": "За семь дней подходящих материалов не найдено.",
    "One source can appear in several branches. See Sources for original headlines and verification notes.":
        "Источник может относиться к нескольким направлениям. Оригиналы заголовков и результаты проверки собраны на листе «Источники».",
    "A source can appear in several branches.": "Один источник может относиться к нескольким направлениям.",
    "Ref": "Код", "Published (MSK)": "Дата публикации (МСК)", "Publisher": "Издатель",
    "English / display headline": "Заголовок на русском", "Original headline": "Заголовок в оригинале",
    "Verification and exposure notes": "Проверка и возможные последствия", "Source URL": "Ссылка на источник",
    "Publisher unlisted": "Издатель не указан", "No upcoming filing-confirmed dates on record.": "Предстоящих дат, подтверждённых раскрытиями, не найдено.",
    "Current": "Текущий период", "1 week ago": "Неделю назад", "1 month ago": "Месяц назад", "3 months ago": "3 месяца назад",
    "6 months ago": "6 месяцев назад", "12 months ago": "12 месяцев назад", "No snapshot": "Нет сохранённой оценки",
    BASIS: "Шкала оценок — от −1 (негативная) до +1 (позитивная). В понедельник учитываются предыдущие семь дней по московскому времени. Для сравнения используются оценки, доступные на тот момент. Для старых оценок указана исходная шкала. Прочерк означает отсутствие оценки.",
    FLOW_NOTE: "Показатели отражают охват источников за неделю публикации. Данные за текущую неделю ещё могут измениться.",
}


def rating_basis(report, language="en"):
    tr = report_translator(language, BRIEF_LABELS)
    if report['run'].get('window_end'):
        if language == "ru":
            return ("Ретроспективная оценка учитывает данные и проверки, доступные на дату выгрузки. "
                    "Шкала — от −1 до +1. В понедельник учитываются предыдущие семь дней по московскому времени. "
                    "Для старых оценок указана исходная шкала. Прочерк означает отсутствие оценки.")
        return ("Retrospective assessment using evidence and reviews available at export time, not knowledge available then. "
                "Scores range from −1 to +1. Monday cutoffs cover the preceding seven days in Moscow. Legacy snapshots retain their labelled original scale. A dash means no rating.")
    return tr(BASIS)


def stamp(report, language="en"):
    if language == "ru":
        status = report_translator(language)(report['run']['status'])
        return f"{report['start']:%d.%m.%Y} – {report['end']:%d.%m.%Y, %H:%M} МСК   |   Поиск №{report['run']['id']} · {status}"
    return f"{report['start']:%d %b %Y} – {report['end']:%d %b %Y, %H:%M} MSK   |   Run {report['run']['id']} · {report['run']['status']}"


def title(s, language="en"):
    a = s["article"]
    if language == "ru":
        return russian_headline(a)
    if review_note(a) and (a.get("headline_review") or {}).get("status") != "faithful":
        return headline(a, "title")
    return headline(a, "title_en") or headline(a, "title")


def source_note(s, language="en"):
    tr = report_translator(language, BRIEF_LABELS)
    a = s["article"]
    v = a.get("verification") or {}
    status = s["state"].replace("_", " ")
    notes = [*s.get("governance", []), *factor_notes(a), status, review_note(a), v.get("reason") or "", *(a.get("exposure_paths") or [])]
    return " · ".join(tr(n) for n in notes if n)


def roster_lines(report, language="en"):
    tr = report_translator(language, BRIEF_LABELS)
    for row in report.get("rosters", []):
        observed = row['observed_at'].astimezone(WEEK_TIMEZONE)
        prior = row['previous_at']
        action = "recorded" if row.get("coverage_note") else "observed"
        label = f"{row['source_label']} · {action} {observed:%d %b %Y}"
        if prior:
            label += f", compared with {prior.astimezone(WEEK_TIMEZONE):%d %b %Y}"
        if language == "ru":
            action = "зафиксировано" if row.get("coverage_note") else "проверено"
            label = f"{row['source_label']} · {action} {observed:%d.%m.%Y}"
            if prior:
                label += f", в сравнении с {prior.astimezone(WEEK_TIMEZONE):%d.%m.%Y}"
        yield label, row['source_url']
        for detail in row['notes']:
            yield tr(detail), ""


def calendar_lines(report, language="en"):
    tr = report_translator(language, BRIEF_LABELS)
    for entry in report["dates"]:
        day = entry['day'].strftime("%d.%m.%Y" if language == "ru" else "%d %b %Y")
        yield f"{day}   ·   {tr(entry['kind'])}   ·   {tr(entry['label'])}", tr(entry["filing"]), entry["source_url"]
    if not report["dates"]:
        yield tr("No upcoming filing-confirmed dates on record."), "", ""


def result_buffer(save):
    buf = BytesIO()
    save(buf)
    buf.seek(0)
    return buf


def company_xlsx(report, language="en"):
    tr = report_translator(language, BRIEF_LABELS)
    book = Workbook()
    ws = book.active
    ws.title = tr("Company brief")
    ws.sheet_view.showGridLines = False
    for col, width in zip("ABCDE", (22, 19, 16, 38, 20)):
        ws.column_dimensions[col].width = width

    def band(text, size=14, color=TEAL):
        ws.append([_safe_excel(text)])
        cell = ws.cell(ws.max_row, 1)
        ws.merge_cells(start_row=cell.row, start_column=1, end_row=cell.row, end_column=5)
        cell.fill = PatternFill("solid", fgColor=color)
        cell.font = Font(name="Aptos Display", size=size, bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True, indent=1)
        ws.row_dimensions[cell.row].height = 48 if size > 14 else 27

    def note(text, link=""):
        ws.append([_safe_excel(text)])
        cell = ws.cell(ws.max_row, 1)
        ws.merge_cells(start_row=cell.row, start_column=1, end_row=cell.row, end_column=5)
        cell.alignment = Alignment(wrap_text=True, vertical="center", indent=1)
        cell.font = Font(name="Aptos", size=11, color=TEAL if link else INK, underline="single" if link else None)
        ws.row_dimensions[cell.row].height = max(27, 16 * (1 + len(text) // 85))
        if link:
            cell.hyperlink = link

    def grid(columns, rows):
        ws.append([tr(column) for column in columns])
        for c in ws[ws.max_row]:
            c.font = Font(bold=True, color=INK)
            c.fill = PatternFill("solid", fgColor=PALE)
        for row in rows:
            ws.append([_safe_excel(tr(c)) if isinstance(c, str) else c for c in row])

    def stories(items):
        for s in items:
            a = s["article"]
            date = a["published_at"].astimezone(WEEK_TIMEZONE)
            day = date.strftime("%d.%m" if language == "ru" else "%d %b")
            note(f"{s['ref']}   {day}   |   {title(s, language)}   ·   {', '.join(map(tr, s['branches']))} · {tr(s['state'].replace('_', ' '))}", a["url"])
        if not items:
            note(tr("No matching headlines in this run’s seven-day window."))

    band(report["subject"]["name"], 18, INK)
    note(tr("Company monitoring report") + "   |   " + stamp(report, language))
    note("  ·  ".join(map(tr, filter(None, [report["subject"].get("country_registration"), report["subject"].get("primary_industries")]))))
    band(tr("Latest relevant headlines"))
    stories(report["top"])
    for branch, heading in BRANCHES.items():
        band(tr(heading))
        stories([s for s in report["sources"] if branch in s["branches"]][:3])
    governance = [s for s in report["sources"] if s.get("governance")]
    if governance or report.get("rosters"):
        band(tr("Leadership and major ownership leads"))
        if governance:
            stories(governance)
        for line, link in roster_lines(report, language):
            note(line, link)
    band(tr("Risk and sentiment history"))
    grid(RATINGS, rating_rows(report, language))
    note(rating_basis(report, language))
    reason, link = driver(report, language)
    note(reason, link)
    band(tr("Next dates to watch"))
    for event, filing, url in calendar_lines(report, language):
        note(event + ("   |   " + filing if filing else ""), url)
    band(tr("How newsflow changed"))
    for label, summary in comparison_rows(report, language):
        note(f"{tr(label)}   |   {summary}")
    note(tr(FLOW_NOTE))
    band(tr("Evidence by branch"))
    grid(COVERAGE, evidence_rows(report))
    note(tr("One source can appear in several branches. See Sources for original headlines and verification notes."))
    if report["errors"]:
        band(tr("Search coverage gaps"))
        for error in report["errors"]:
            note(tr(error))
    ws.freeze_panes = "A3"
    src = book.create_sheet(tr("Sources"))
    src.sheet_view.showGridLines = False
    src.append([tr(c) for c in ("Ref", "Published (MSK)", "Branch", "Publisher", "English / display headline", "Original headline",
                "Language", "Source check", "Verification and exposure notes", "Source URL")])
    for s in report["sources"]:
        a = s["article"]
        url = (a.get("verification") or {}).get("source_url") or a["url"]
        src.append([_safe_excel(value) for value in (s["ref"], a["published_at"].astimezone(WEEK_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                    ", ".join(map(tr, s["branches"])), a["publisher"], title(s, language), a["title"], a["language"], tr(s["state"]), source_note(s, language), url)])
        src.cell(src.max_row, 10).hyperlink = url
        src.cell(src.max_row, 10).style = "Hyperlink"
        src.row_dimensions[src.max_row].height = max(46, 15 * (1 + max(len(src.cell(src.max_row, 9).value or "") // 68, len(a["title"]) // 58)))
        for cell in src[src.max_row]:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    for cell in src[1]:
        cell.fill = PatternFill("solid", fgColor=INK)
        cell.font = Font(bold=True, color="FFFFFF")
    for col, width in zip("ABCDEFGHIJ", (9, 22, 27, 25, 64, 64, 12, 20, 76, 48)):
        src.column_dimensions[col].width = width
    src.freeze_panes = "C2"
    src.auto_filter.ref = src.dimensions
    return result_buffer(book.save)


def company_pdf(report, language="en"):
    tr = report_translator(language, BRIEF_LABELS)
    font = _font_name()
    def para(text, size=10, color=INK, after=4, link=""):
        content = escape(str(text))
        if link and link.startswith(("http://", "https://")):
            content = f"<link href={quoteattr(link)} color='#{TEAL}'>{content}</link>"
        style = ParagraphStyle("report", fontName=font, fontSize=size, leading=size*1.32,
                               spaceAfter=after, textColor=colors.HexColor("#" + color))
        return Paragraph(content, style)

    def heading(text):
        p = para(tr(text), 15, TEAL, 7)
        p.style.spaceBefore = 9
        p.keepWithNext = True
        return p

    def grid(columns, rows, widths):
        table = Table([[para(tr(c), 8, "FFFFFF", 0) for c in columns]] +
                      [[para(tr(c), 9, INK, 0) for c in row] for row in rows], colWidths=widths, hAlign="LEFT", repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#"+INK)),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#"+PALE), colors.white]),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        return table

    subject = report["subject"]
    story = [para(tr("Company monitoring report").upper(), 9, TEAL, 7), para(subject["name"], 22, INK, 6),
             para(stamp(report, language), 9, MUTED), para("  ·  ".join(map(tr, filter(None, [subject.get("country_registration"),
                  subject.get("primary_industries")]))), 10, MUTED), heading("Latest relevant headlines")]
    cited = {}
    def stories(items):
        for s in items:
            a = s["article"]
            cited[s["ref"]] = s
            date = a["published_at"].astimezone(WEEK_TIMEZONE)
            day = date.strftime("%d.%m" if language == "ru" else "%d %b")
            story.append(para(f"{day}   {title(s, language)}  [{s['ref']}]", 10, INK, 2, a["url"]))
            story.append(para(f"{a['publisher'] or tr('Publisher unlisted')}   ·   {', '.join(map(tr, s['branches']))}   ·   {tr(s['state'].replace('_',' '))}", 8, MUTED, 6))
        if not items:
            story.append(para(tr("No matching headlines in the seven-day window."), 9, MUTED))
    stories(report["top"])
    story.extend([heading("Risk and sentiment history"), grid(RATINGS, rating_rows(report, language), [98, 96, 55, 160, 88]),
                  para(rating_basis(report, language), 8, MUTED)])
    reason, url = driver(report, language)
    story.append(para(reason, 10, INK, 4, url))
    story.append(heading("Next dates to watch"))
    for event, filing, url in calendar_lines(report, language):
        story.append(para(event, 10, INK))
        if filing:
            story.append(para(filing, 9, TEAL, 4, url))
    story.append(PageBreak())
    story.append(heading("How newsflow changed"))
    for label, summary in comparison_rows(report, language):
        story.append(para(f"{tr(label)}   ·   {summary}"))
    story.append(para(tr(FLOW_NOTE), 8, MUTED))
    for branch, section in BRANCHES.items():
        story.append(heading(section))
        selected = [s for s in report["sources"] if branch in s["branches"]][:3]
        stories(selected)
        if branch != "Company" and selected:
            for path in dict.fromkeys(path for s in selected for path in s["article"]["exposure_paths"]
                                      if path.startswith(branch.split()[0] + " ·")):
                story.append(para(tr(path), 9, MUTED))
    governance = [s for s in report["sources"] if s.get("governance")]
    if governance or report.get("rosters"):
        story.append(heading("Leadership and major ownership leads"))
        if governance:
            stories(governance)
        for line, link in roster_lines(report, language):
            story.append(para(line, 9, INK, 4, link))
    story.extend([heading("Evidence by branch"), grid(COVERAGE, evidence_rows(report), [131, 119, 125, 122]),
                  para(tr("A source can appear in several branches."), 8, MUTED)])
    story.extend([heading("Sources and verification notes"),
                  para(f"Использовано источников: {len(cited)}. Всего материалов по теме за период: {len(report['sources'])}. Полный список доступен в Excel."
                       if language == "ru" else f"{len(cited)} cited sources from {len(report['sources'])} relevant in-window results. "
                       "The workbook lists all relevant sources.", 9, MUTED)])
    for ref, s in cited.items():
        a = s["article"]
        v = a.get("verification") or {}
        story.append(para(f"{ref}   {a['publisher'] or tr('Publisher unlisted')}   ·   {a['language']}   |   {title(s, language) if language == 'ru' else headline(a, 'title')}",
                          10, INK, 2, v.get("source_url") or a["url"]))
        story.append(para(source_note(s, language), 9, MUTED, 8))
    if report["errors"]:
        story.append(heading("Search coverage gaps"))
        story.extend(para(tr(error), 9, MUTED) for error in report["errors"])

    def footer(canvas, doc):
        w,h = A4
        canvas.setFillColor(colors.HexColor("#"+TEAL))
        canvas.rect(44, h-27, w-88, 2, fill=1, stroke=0)
        canvas.setFont(font, 8)
        canvas.drawString(44, 24, f"RA | Кредитный мониторинг   /   Поиск №{report['run']['id']}" if language == "ru" else f"RA Credit Monitor   /   Run {report['run']['id']}")
        canvas.drawRightString(w-44, 24, str(doc.page))

    def save(buf):
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=43, rightMargin=43, topMargin=39, bottomMargin=41,
                                title=subject["name"] + " | " + tr("Company monitoring report"))
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return result_buffer(save)
