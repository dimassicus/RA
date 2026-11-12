"""Downloadable, explicitly unverified search-lead reports."""

from __future__ import annotations

from datetime import timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from .risk_factors import notes as factor_notes
from .headline_review import review_note
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle


WARNING = (
    "Unverified discovery leads. Confirm legal entity, original publisher, document content, "
    "event date, and subsequent developments before treating any item as a credit event."
)


REPORT_LABELS = {
    "Summary": "Сводка", "Run ID": "Номер поиска", "Created (UTC)": "Время создания (UTC)",
    "Scope": "Охват поиска", "Status": "Статус", "Companies searched": "Проверено компаний",
    "Analyst-selected leads": "Материалы, отобранные аналитиком", "News/disclosure leads": "Новости и раскрытия",
    "Source errors": "Ошибки источников", "New to system": "Впервые в системе",
    "Official notices": "Официальные сообщения", "Important": "Важно", "Selected leads": "Отобранные материалы",
    "Leads": "Материалы", "Published": "Дата публикации", "Company": "Компания", "Source type": "Тип источника",
    "Publisher (original)": "Издатель (оригинал)", "Publisher (English)": "Название издателя",
    "Headline (original)": "Заголовок (оригинал)", "Headline (English, machine translation)": "Заголовок на русском",
    "Review cues": "Признаки для проверки", "Discovery status": "Статус обнаружения", "Language": "Язык источника",
    "Article / notice URL": "Ссылка на материал", "Search query": "Поисковый запрос", "Discovery service": "Сервис поиска",
    "Article sentiment (-1 to +1)": "Тональность материала (от −1 до +1)", "Score status": "Статус оценки",
    "Eligible as event evidence": "Учитывается при оценке события", "Score reason": "Обоснование оценки",
    "Scoring version": "Версия методики", "Source check": "Проверка источника", "Source check reason": "Причина решения",
    "Event group": "Группа событий", "Potential company exposure": "Возможные последствия для компании",
    "Headline translation review": "Проверка английского перевода", "Possible risk themes and evidence": "Возможные риски и подтверждения",
    "Official notice": "Официальное сообщение", "News lead": "Новость", "Seen before": "Найден ранее",
    "Company / run": "Компания / поиск", "Error": "Ошибка", "Captured (UTC)": "Время регистрации (UTC)",
    "Yes": "Да", "No": "Нет", "Date": "Дата", "Source": "Источник", "Headline / English and original": "Заголовок / перевод и оригинал",
    "Official": "Официальный источник", "Unknown": "Неизвестно", "Original": "Оригинал", "via": "через",
    "Source access errors": "Ошибки доступа к источникам", "Page": "Страница", "Run": "Поиск",
    "RA | Analyst-selected lead report": "RA | Отчёт по отобранным материалам",
    "RA | Search lead report": "RA | Отчёт по результатам поиска",
    "RA Credit Monitor  |  Unverified search leads": "RA | Кредитный мониторинг  |  Непроверенные материалы",
    "These leads were selected by an analyst. Selection does not establish that an event is verified.":
        "Материалы отобраны аналитиком. Это не подтверждает достоверность события.",
    "New company-matched risk leads published within 30 days of the search. The Excel download includes every search lead. Headline triage is not a verified risk finding.":
        "Новые материалы о рисках компании, опубликованные за 30 дней до поиска. Все материалы доступны в Excel. Отбор по заголовку не подтверждает наличие риска.",
    "PDF selection capped at 300 rows; see Excel for all leads.": "В PDF включены первые 300 материалов. Полный список доступен в Excel.",
    "No new official or keyword-matched leads were selected for this PDF.": "Для этого отчёта не отобрано новых официальных сообщений или материалов с ключевыми словами.",
    WARNING: "Материалы поиска не проверены. До признания кредитного события проверьте юридическое лицо, первоисточник, содержание документа, дату события и последующие изменения.",
    "all": "Весь список наблюдения", "selected": "Выбранные компании", "one_off": "Разовый поиск",
    "queued": "В очереди", "running": "Выполняется", "completed": "Завершён", "partial": "Частично завершён",
    "failed": "Ошибка", "pending": "Ожидает проверки", "provisional": "Предварительная оценка",
    "needs_review": "Требуется проверка", "reviewed": "Проверено", "supported": "Подтверждено",
    "unmatched": "Компания не совпадает", "English translation unavailable": "Перевод на английский недоступен",
}


def report_translator(language, labels=None):
    if language != "ru":
        return lambda value: value
    from .i18n import ru
    def translate(value):
        if not isinstance(value, str) or not any(c.isalpha() for c in value):
            return value
        return (labels or {}).get(value) or REPORT_LABELS.get(value) or ru(value)
    return translate


def russian_headline(article):
    from .i18n import headline
    return headline(article, target="ru")


def keyword_notes(article):
    from .relevance import headline
    from .keywords import matches
    hits = matches(headline(article, "title"), article.get("language")) + matches(headline(article, "title_en"))
    return [f"{term} ({direction})" for term, _, direction in sorted(set(hits))]


def _date(value) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d") if value else ""


def _utc_timestamp(value) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _safe_excel(value) -> str:
    text = str(value or "")
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def make_xlsx(run: dict, results: list[dict], errors: list[dict], selected: bool = False, language: str = "en") -> BytesIO:
    tr = report_translator(language)
    book = Workbook()
    book.properties.title = f"RA {'selected report' if selected else 'search leads'} - run {run['id']}"
    if language == "ru":
        book.properties.title = f"RA | Отчёт по поиску №{run['id']}"
    book.properties.creator = "RA Credit Monitor"
    summary = book.active
    summary.title = tr("Summary")
    summary.merge_cells("A1:D1")
    summary["A1"] = tr("RA | Analyst-selected lead report" if selected else "RA | Search lead report")
    summary["A1"].font = Font(name="Aptos Display", size=19, bold=True, color="FFFFFF")
    summary["A1"].fill = PatternFill("solid", fgColor="0F6F69")
    summary.row_dimensions[1].height = 35
    details = [
        ("Run ID", run["id"]), ("Created (UTC)", _utc_timestamp(run["created_at"])),
        ("Scope", run["scope"]), ("Status", run["status"]),
        ("Companies searched", run["completed_companies"]),
        (("Analyst-selected leads" if selected else "News/disclosure leads"), len(results)), ("Source errors", sum(not e.get("resolved") for e in errors)),
        ("New to system", sum(not item["seen_before"] for item in results)),
        ("Official notices", sum(item["source_kind"] == "official" for item in results)),
    ]
    for row_index, (label, value) in enumerate(details, start=3):
        summary.cell(row_index, 1, tr(label)).font = Font(bold=True, color="355E58")
        summary.cell(row_index, 2, tr(value) if label in ("Scope", "Status") else value)
    summary["A14"] = tr("Important")
    summary["A14"].font = Font(bold=True, color="A34735")
    summary.merge_cells("A15:D17")
    summary["A15"] = tr(WARNING)
    summary["A15"].alignment = Alignment(wrap_text=True, vertical="top")
    summary.column_dimensions["A"].width = 25
    summary.column_dimensions["B"].width = 35
    summary.column_dimensions["C"].width = 20
    summary.column_dimensions["D"].width = 20

    leads = book.create_sheet(tr("Selected leads" if selected else "Leads"))
    columns = [
        "Published", "Company", "Source type", "Publisher (original)", "Publisher (English)",
        "Headline (original)", "Headline (English, machine translation)", "Review cues",
        "Discovery status", "Language", "Article / notice URL", "Search query", "Discovery service",
        "Article sentiment (-1 to +1)", "Score status", "Eligible as event evidence", "Score reason", "Scoring version", "Source check", "Source check reason", "Event group", "Potential company exposure", "Headline translation review", "Possible risk themes and evidence",
    ]
    leads.append([tr(label) for label in columns])
    for cell in leads[1]:
        cell.fill = PatternFill("solid", fgColor="0F6F69")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    leads.row_dimensions[1].height = 28
    for item in results:
        leads.append([
            _date(item["published_at"]), _safe_excel(item["subject_name"]),
            tr("Official notice" if item["source_kind"] == "official" else "News lead"),
            _safe_excel(item["publisher"]), _safe_excel(item.get("publisher_en")),
            _safe_excel(item["title"]), _safe_excel(russian_headline(item) if language == "ru" else item.get("title_en")),
            _safe_excel(", ".join(map(tr, keyword_notes(item)))), tr("Seen before" if item["seen_before"] else "New to system"),
            item["language"], item["url"], _safe_excel(item["query"]),
            _safe_excel(item.get("discovery_service", "")),
            item.get("score"), _safe_excel(tr(item.get("score_status", "pending"))),
            tr("Yes" if item.get("score_included") else "No"), _safe_excel(tr(item.get("score_reason", ""))),
            _safe_excel(item.get("score_version", "")),
            _safe_excel(tr(item.get("verification", {}).get("status", "pending"))),
            _safe_excel(tr(item.get("verification", {}).get("reason", ""))), _safe_excel(item.get("event_group", "")),
            _safe_excel("\n".join(map(tr, item.get("exposure_paths", [])))), _safe_excel(tr(review_note(item))), _safe_excel("\n".join(map(tr, factor_notes(item)))),
        ])
        row = leads.max_row
        link = leads.cell(row, 11)
        link.hyperlink = item["url"]
        link.font = Font(color="056D84", underline="single")
        if item["source_kind"] == "official":
            leads.cell(row, 3).fill = PatternFill("solid", fgColor="E3F5E9")
        for cell in leads[row]:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    leads.freeze_panes = "A2"
    leads.auto_filter.ref = leads.dimensions
    for index, width in enumerate((15, 29, 18, 22, 22, 70, 70, 28, 19, 14, 55, 40, 22, 15, 18, 20, 60, 22, 18, 60, 24, 64, 28, 70), start=1):
        leads.column_dimensions[get_column_letter(index)].width = width
    if errors:
        failure_sheet = book.create_sheet(tr("Source errors"))
        failure_sheet.append([tr(label) for label in ("Company / run", "Error", "Captured (UTC)")])
        for error in errors:
            failure_sheet.append([
                _safe_excel(error["subject_name"]), _safe_excel(tr(error["message"])),
                _utc_timestamp(error["created_at"]),
            ])
        failure_sheet.column_dimensions["A"].width = 28
        failure_sheet.column_dimensions["B"].width = 95
        failure_sheet.column_dimensions["C"].width = 23
        failure_sheet.freeze_panes = "A2"
    output = BytesIO()
    book.save(output)
    output.seek(0)
    return output


def _font_name() -> str:
    for font, path in (
        ("RAArial", "/System/Library/Fonts/Supplemental/Arial.ttf"),
        ("RADejaVu", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        ("RALiberation", "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        ("RAArial", "C:/Windows/Fonts/arial.ttf"),
    ):
        if font in pdfmetrics.getRegisteredFontNames():
            return font
        if Path(path).is_file():
            pdfmetrics.registerFont(TTFont(font, path))
            return font
    return "Helvetica"


def make_pdf(run: dict, results: list[dict], errors: list[dict], selected: bool = False, language: str = "en") -> BytesIO:
    tr = report_translator(language)
    output = BytesIO()
    font = _font_name()
    doc = SimpleDocTemplate(
        output, pagesize=landscape(A4), rightMargin=30, leftMargin=30,
        topMargin=35, bottomMargin=34, title=f"RA | Поиск №{run['id']}" if language == "ru" else f"RA search leads - run {run['id']}",
    )
    title_style = ParagraphStyle("title", fontName=font, fontSize=18, leading=22, textColor=colors.HexColor("#163134"))
    meta_style = ParagraphStyle("meta", fontName=font, fontSize=9, leading=13, textColor=colors.HexColor("#4C6665"))
    note_style = ParagraphStyle("note", fontName=font, fontSize=8, leading=11, textColor=colors.HexColor("#8D4739"))
    cell_style = ParagraphStyle("cell", fontName=font, fontSize=7.3, leading=10, alignment=TA_LEFT)
    head_style = ParagraphStyle("head", fontName=font, fontSize=7.3, leading=10, textColor=colors.white)

    from .relevance import classify_results
    results = classify_results(run, results)
    new_results = [item for item in results if not item["seen_before"]]
    eligible = results if selected else [item for item in new_results if item["relevance"] == "risk"]
    pdf_rows = eligible[:300]
    story = [
        Paragraph(tr("RA | Analyst-selected lead report" if selected else "RA | Search lead report"), title_style), Spacer(1, 8),
        Paragraph(
            f"{tr('Run')} #{run['id']}  |  {escape(tr(run['scope']) if language == 'ru' else run['scope'].replace('_', ' ').title())}  |  "
            f"{run['created_at'].astimezone(timezone.utc).strftime('%d.%m.%Y %H:%M UTC' if language == 'ru' else '%d %b %Y %H:%M UTC')}  |  {escape(tr(run['status']))}", meta_style,
        ), Spacer(1, 5),
        Paragraph(
            (f"Проверено компаний: {run['completed_companies']}  |  Всего материалов: {len(results)}  |  "
             f"Новых: {len(new_results)}  |  Ошибок источников: {sum(not e.get('resolved') for e in errors)}" if language == "ru" else
             f"{run['completed_companies']} {'company' if run['completed_companies'] == 1 else 'companies'} searched  |  {len(results)} total leads  |  "
            f"{len(new_results)} new to system  |  {sum(not e.get('resolved') for e in errors)} source errors"), meta_style,
        ), Spacer(1, 12), Paragraph(escape(tr(WARNING)), note_style), Spacer(1, 12),
        Paragraph(
            tr("These leads were selected by an analyst. Selection does not establish that an event is verified."
             if selected else "New company-matched risk leads published within 30 days of the search. "
             "The Excel download includes every search lead. Headline triage is not a verified risk finding."), meta_style,
        ), Spacer(1, 12),
    ]
    if len(eligible) > 300:
        story.extend([Paragraph(tr("PDF selection capped at 300 rows; see Excel for all leads."), note_style), Spacer(1, 8)])
    if pdf_rows:
        rows = [[Paragraph(tr(value), head_style) for value in ("Date", "Company", "Source", "Headline / English and original", "Review cues")]]
        for item in pdf_rows:
            title = escape(item["title"])
            display = escape(russian_headline(item) if language == "ru" else item.get("title_en") or "")
            url = quoteattr(item["url"])
            bilingual = f"<b>{display}</b><br/>{tr('Original')}: {title}" if display and display != title else title
            if review_note(item):
                bilingual += "<br/>" + escape(tr(review_note(item)))
            rows.append([
                Paragraph(_date(item["published_at"]) or tr("Unknown"), cell_style),
                Paragraph(escape(item["subject_name"]), cell_style),
                Paragraph(
                    (tr("Official") if item["source_kind"] == "official" else escape(item["publisher"]))
                    + ("<br/>" + tr("via") + " " + escape(item["discovery_service"]) if item.get("discovery_service") else ""),
                    cell_style,
                ),
                Paragraph(f"<link href={url} color='#0F6F69'>{bilingual}</link>", cell_style),
                Paragraph("<br/>".join(escape(tr(text)) for text in [*keyword_notes(item), *item.get("exposure_paths", [])]) or "-", cell_style),
            ])
        table = LongTable(rows, colWidths=[69, 120, 87, 354, 128], repeatRows=1, hAlign="LEFT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F6F69")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#0F6F69")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F7F4")]),
        ]))
        story.append(table)
    else:
        story.append(Paragraph(tr("No new official or keyword-matched leads were selected for this PDF."), meta_style))
    if errors:
        story.extend([Spacer(1, 13), Paragraph(tr("Source access errors"), title_style), Spacer(1, 6)])
        for error in errors[:30]:
            story.append(Paragraph(escape(f"{error['subject_name']}: {tr(error['message'])}"), note_style))

    def footer(canvas, document):
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#728581"))
        canvas.drawString(30, 18, tr("RA Credit Monitor  |  Unverified search leads"))
        canvas.drawRightString(document.pagesize[0] - 30, 18, f"{tr('Page')} {document.page}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
