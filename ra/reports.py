"""Downloadable, explicitly unverified search-lead reports."""

from __future__ import annotations

from datetime import timezone
from io import BytesIO
from xml.sax.saxutils import escape, quoteattr

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


def _date(value) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d") if value else ""


def _utc_timestamp(value) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _safe_excel(value) -> str:
    text = str(value or "")
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def make_xlsx(run: dict, results: list[dict], errors: list[dict], selected: bool = False) -> BytesIO:
    book = Workbook()
    book.properties.title = f"RA {'selected report' if selected else 'search leads'} - run {run['id']}"
    book.properties.creator = "RA Credit Monitor"
    summary = book.active
    summary.title = "Summary"
    summary.merge_cells("A1:D1")
    summary["A1"] = "RA | Analyst-selected lead report" if selected else "RA | Search lead report"
    summary["A1"].font = Font(name="Aptos Display", size=19, bold=True, color="FFFFFF")
    summary["A1"].fill = PatternFill("solid", fgColor="0F6F69")
    summary.row_dimensions[1].height = 35
    details = [
        ("Run ID", run["id"]), ("Created (UTC)", _utc_timestamp(run["created_at"])),
        ("Scope", run["scope"]), ("Status", run["status"]),
        ("Companies searched", run["completed_companies"]),
        (("Analyst-selected leads" if selected else "News/disclosure leads"), len(results)), ("Source errors", len(errors)),
        ("New to system", sum(not item["seen_before"] for item in results)),
        ("Official notices", sum(item["source_kind"] == "official" for item in results)),
    ]
    for row_index, (label, value) in enumerate(details, start=3):
        summary.cell(row_index, 1, label).font = Font(bold=True, color="355E58")
        summary.cell(row_index, 2, value)
    summary["A14"] = "Important"
    summary["A14"].font = Font(bold=True, color="A34735")
    summary.merge_cells("A15:D17")
    summary["A15"] = WARNING
    summary["A15"].alignment = Alignment(wrap_text=True, vertical="top")
    summary.column_dimensions["A"].width = 25
    summary.column_dimensions["B"].width = 35
    summary.column_dimensions["C"].width = 20
    summary.column_dimensions["D"].width = 20

    leads = book.create_sheet("Selected leads" if selected else "Leads")
    columns = [
        "Published", "Company", "Source type", "Publisher (original)", "Publisher (English)",
        "Headline (original)", "Headline (English, machine translation)", "Review cues",
        "Discovery status", "Language", "Article / notice URL", "Search query",
    ]
    leads.append(columns)
    for cell in leads[1]:
        cell.fill = PatternFill("solid", fgColor="0F6F69")
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    leads.row_dimensions[1].height = 28
    for item in results:
        leads.append([
            _date(item["published_at"]), _safe_excel(item["subject_name"]),
            "Official notice" if item["source_kind"] == "official" else "News lead",
            _safe_excel(item["publisher"]), _safe_excel(item.get("publisher_en")),
            _safe_excel(item["title"]), _safe_excel(item.get("title_en")),
            ", ".join(item["signal_terms"]), "Seen before" if item["seen_before"] else "New to system",
            item["language"], item["url"], _safe_excel(item["query"]),
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
    leads.auto_filter.ref = f"A1:L{max(1, leads.max_row)}"
    for index, width in enumerate((15, 29, 18, 22, 22, 70, 70, 28, 19, 14, 55, 40), start=1):
        leads.column_dimensions[get_column_letter(index)].width = width
    if errors:
        failure_sheet = book.create_sheet("Source errors")
        failure_sheet.append(["Company / run", "Error", "Captured (UTC)"])
        for error in errors:
            failure_sheet.append([
                _safe_excel(error["subject_name"]), _safe_excel(error["message"]),
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
    path = "/System/Library/Fonts/Supplemental/Arial.ttf"
    try:
        if "RAArial" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("RAArial", path))
        return "RAArial"
    except (OSError, ValueError):
        return "Helvetica"


def make_pdf(run: dict, results: list[dict], errors: list[dict], selected: bool = False) -> BytesIO:
    output = BytesIO()
    font = _font_name()
    doc = SimpleDocTemplate(
        output, pagesize=landscape(A4), rightMargin=30, leftMargin=30,
        topMargin=35, bottomMargin=34, title=f"RA search leads - run {run['id']}",
    )
    title_style = ParagraphStyle("title", fontName=font, fontSize=18, leading=22, textColor=colors.HexColor("#163134"))
    meta_style = ParagraphStyle("meta", fontName=font, fontSize=9, leading=13, textColor=colors.HexColor("#4C6665"))
    note_style = ParagraphStyle("note", fontName=font, fontSize=8, leading=11, textColor=colors.HexColor("#8D4739"))
    cell_style = ParagraphStyle("cell", fontName=font, fontSize=7.3, leading=10, alignment=TA_LEFT)
    head_style = ParagraphStyle("head", fontName=font, fontSize=7.3, leading=10, textColor=colors.white)

    new_results = [item for item in results if not item["seen_before"]]
    eligible = results if selected else [item for item in new_results if item["source_kind"] == "official" or item["signal_terms"]]
    pdf_rows = eligible[:300]
    story = [
        Paragraph("RA | Analyst-selected lead report" if selected else "RA | Search lead report", title_style), Spacer(1, 8),
        Paragraph(
            f"Run #{run['id']}  |  {escape(run['scope'].replace('_', ' ').title())}  |  "
            f"{run['created_at'].astimezone(timezone.utc).strftime('%d %b %Y %H:%M UTC')}  |  {escape(run['status'])}", meta_style,
        ), Spacer(1, 5),
        Paragraph(
            f"{run['completed_companies']} {'company' if run['completed_companies'] == 1 else 'companies'} searched  |  {len(results)} total leads  |  "
            f"{len(new_results)} new to system  |  {len(errors)} source errors", meta_style,
        ), Spacer(1, 12), Paragraph(escape(WARNING), note_style), Spacer(1, 12),
        Paragraph(
            ("These leads were selected by an analyst. Selection does not establish that an event is verified."
             if selected else "Selected new leads: official notices and headlines matching a review keyword. "
             "The Excel download includes every search lead. A keyword is not a risk finding."), meta_style,
        ), Spacer(1, 12),
    ]
    if len(eligible) > 300:
        story.extend([Paragraph("PDF selection capped at 300 rows; see Excel for all leads.", note_style), Spacer(1, 8)])
    if pdf_rows:
        rows = [[Paragraph(value, head_style) for value in ("Date", "Company", "Source", "Headline / English and original", "Review cues")]]
        for item in pdf_rows:
            title = escape(item["title"])
            title_en = escape(item.get("title_en") or "")
            url = quoteattr(item["url"])
            bilingual = f"<b>{title_en}</b><br/>Original: {title}" if title_en and title_en != title else title
            rows.append([
                Paragraph(_date(item["published_at"]) or "Unknown", cell_style),
                Paragraph(escape(item["subject_name"]), cell_style),
                Paragraph("Official" if item["source_kind"] == "official" else escape(item["publisher"]), cell_style),
                Paragraph(f"<link href={url} color='#0F6F69'>{bilingual}</link>", cell_style),
                Paragraph(escape(", ".join(item["signal_terms"])) or "-", cell_style),
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
        story.append(Paragraph("No new official or keyword-matched leads were selected for this PDF.", meta_style))
    if errors:
        story.extend([Spacer(1, 13), Paragraph("Source access errors", title_style), Spacer(1, 6)])
        for error in errors[:30]:
            story.append(Paragraph(escape(f"{error['subject_name']}: {error['message']}"), note_style))

    def footer(canvas, document):
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#728581"))
        canvas.drawString(30, 18, "RA Credit Monitor  |  Unverified search leads")
        canvas.drawRightString(document.pagesize[0] - 30, 18, f"Page {document.page}")

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    output.seek(0)
    return output
