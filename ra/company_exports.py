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
from .company_report import driver, evidence_rows, rating_rows
from .relevance import headline
from .reports import _font_name, _safe_excel
from .scoring import WEEK_TIMEZONE


INK, TEAL, MUTED, PALE = "18373B", "096B67", "496368", "EAF3F1"
BRANCHES = {"Company": "Company news", "Sector": "Sector news", "Country / region": "Country and region news"}
RATINGS = ("Period", "Week of", "Score", "Search coverage", "Scored events")
COVERAGE = ("Branch", "Relevant sources", "Impact supported", "Awaiting check")
BASIS = ("Scores range from −3 (most adverse) to +3 (most favourable). Weeks start Monday in Moscow. "
         "Earlier ratings use the snapshot available at the comparison time. A dash means no rating.")
FLOW_NOTE = "Counts reflect coverage accumulated in each publication week. The current week may still be in progress."


def stamp(report):
    return f"{report['start']:%d %b %Y} – {report['end']:%d %b %Y, %H:%M} MSK   |   Run {report['run']['id']} · {report['run']['status']}"


def title(s):
    a = s["article"]
    if review_note(a) and (a.get("headline_review") or {}).get("status") != "faithful":
        return headline(a, "title")
    return headline(a, "title_en") or headline(a, "title")


def source_note(s):
    a = s["article"]
    v = a.get("verification") or {}
    status = s["state"].replace("_", " ")
    notes = [*s.get("governance", []), *factor_notes(a), status, review_note(a), v.get("reason") or "", *(a.get("exposure_paths") or [])]
    return " · ".join(n for n in notes if n)


def roster_lines(report):
    for row in report.get("rosters", []):
        observed = row['observed_at'].astimezone(WEEK_TIMEZONE)
        prior = row['previous_at']
        action = "recorded" if row.get("coverage_note") else "observed"
        label = f"{row['source_label']} · {action} {observed:%d %b %Y}"
        if prior:
            label += f", compared with {prior.astimezone(WEEK_TIMEZONE):%d %b %Y}"
        yield label, row['source_url']
        for detail in row['notes']:
            yield detail, ""


def calendar_lines(report):
    for entry in report["dates"]:
        yield (f"{entry['day']:%d %b %Y}   ·   {entry['kind']}   ·   {entry['label']}",
               entry["filing"], entry["source_url"])
    if not report["dates"]:
        yield "No upcoming filing-confirmed dates on record.", "", ""


def result_buffer(save):
    buf = BytesIO()
    save(buf)
    buf.seek(0)
    return buf


def company_xlsx(report):
    book = Workbook()
    ws = book.active
    ws.title = "Company brief"
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
        ws.append(columns)
        for c in ws[ws.max_row]:
            c.font = Font(bold=True, color=INK)
            c.fill = PatternFill("solid", fgColor=PALE)
        for row in rows:
            ws.append(list(row))

    def stories(items):
        for s in items:
            a = s["article"]
            date = a["published_at"].astimezone(WEEK_TIMEZONE)
            note(f"{s['ref']}   {date:%d %b}   |   {title(s)}   ·   {', '.join(s['branches'])} · {s['state'].replace('_', ' ')}", a["url"])
        if not items:
            note("No matching headlines in this run’s seven-day window.")

    band(report["subject"]["name"], 18, INK)
    note("Company monitoring report   |   " + stamp(report))
    note("  ·  ".join(filter(None, [report["subject"].get("country_registration"), report["subject"].get("primary_industries")])))
    band("Latest relevant headlines")
    stories(report["top"])
    for branch, heading in BRANCHES.items():
        band(heading)
        stories([s for s in report["sources"] if branch in s["branches"]][:3])
    governance = [s for s in report["sources"] if s.get("governance")]
    if governance or report.get("rosters"):
        band("Leadership and major ownership leads")
        if governance:
            stories(governance)
        for line, link in roster_lines(report):
            note(line, link)
    band("Risk and sentiment history")
    grid(RATINGS, rating_rows(report))
    note(BASIS)
    note(*driver(report))
    band("Next dates to watch")
    for event, filing, url in calendar_lines(report):
        note(event + ("   |   " + filing if filing else ""), url)
    band("How newsflow changed")
    for label, summary in report["comparisons"]:
        note(f"{label}   |   {summary}")
    note(FLOW_NOTE)
    band("Evidence by branch")
    grid(COVERAGE, evidence_rows(report))
    note("One source can appear in several branches. See Sources for original headlines and verification notes.")
    if report["errors"]:
        band("Search coverage gaps")
        for error in report["errors"]:
            note(error)
    ws.freeze_panes = "A3"
    src = book.create_sheet("Sources")
    src.sheet_view.showGridLines = False
    src.append(("Ref", "Published (MSK)", "Branch", "Publisher", "English / display headline", "Original headline",
                "Language", "Source check", "Verification and exposure notes", "Source URL"))
    for s in report["sources"]:
        a = s["article"]
        url = (a.get("verification") or {}).get("source_url") or a["url"]
        src.append([_safe_excel(value) for value in (s["ref"], a["published_at"].astimezone(WEEK_TIMEZONE).strftime("%Y-%m-%d %H:%M"),
                    ", ".join(s["branches"]), a["publisher"], title(s), a["title"], a["language"], s["state"], source_note(s), url)])
        src.cell(src.max_row, 10).hyperlink = url
        src.cell(src.max_row, 10).style = "Hyperlink"
        src.row_dimensions[src.max_row].height = max(46, 15 * (1 + max(len(source_note(s)) // 68, len(a["title"]) // 58)))
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


def company_pdf(report):
    font = _font_name()
    def para(text, size=10, color=INK, after=4, link=""):
        content = escape(str(text))
        if link and link.startswith(("http://", "https://")):
            content = f"<link href={quoteattr(link)} color='#{TEAL}'>{content}</link>"
        style = ParagraphStyle("report", fontName=font, fontSize=size, leading=size*1.32,
                               spaceAfter=after, textColor=colors.HexColor("#" + color))
        return Paragraph(content, style)

    def heading(text):
        p = para(text, 15, TEAL, 7)
        p.style.spaceBefore = 9
        p.keepWithNext = True
        return p

    def grid(columns, rows, widths):
        table = Table([[para(c, 8, "FFFFFF", 0) for c in columns]] +
                      [[para(c, 9, INK, 0) for c in row] for row in rows], colWidths=widths, hAlign="LEFT", repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#"+INK)),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#"+PALE), colors.white]),
                                   ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                   ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        return table

    subject = report["subject"]
    story = [para("COMPANY MONITORING REPORT", 9, TEAL, 7), para(subject["name"], 22, INK, 6),
             para(stamp(report), 9, MUTED), para("  ·  ".join(filter(None, [subject.get("country_registration"),
                  subject.get("primary_industries")])), 10, MUTED), heading("Latest relevant headlines")]
    cited = {}
    def stories(items):
        for s in items:
            a = s["article"]
            cited[s["ref"]] = s
            date = a["published_at"].astimezone(WEEK_TIMEZONE)
            story.append(para(f"{date:%d %b}   {title(s)}  [{s['ref']}]", 10, INK, 2, a["url"]))
            story.append(para(f"{a['publisher'] or 'Publisher unlisted'}   ·   {', '.join(s['branches'])}   ·   {s['state'].replace('_',' ')}", 8, MUTED, 6))
        if not items:
            story.append(para("No matching headlines in the seven-day window.", 9, MUTED))
    stories(report["top"])
    story.extend([heading("Risk and sentiment history"), grid(RATINGS, rating_rows(report), [98, 96, 55, 160, 88]),
                  para(BASIS, 8, MUTED)])
    reason, url = driver(report)
    story.append(para(reason, 10, INK, 4, url))
    story.append(heading("Next dates to watch"))
    for event, filing, url in calendar_lines(report):
        story.append(para(event, 10, INK))
        if filing:
            story.append(para(filing, 9, TEAL, 4, url))
    story.append(PageBreak())
    story.append(heading("How newsflow changed"))
    for label, summary in report["comparisons"]:
        story.append(para(f"{label}   ·   {summary}"))
    story.append(para(FLOW_NOTE, 8, MUTED))
    for branch, section in BRANCHES.items():
        story.append(heading(section))
        selected = [s for s in report["sources"] if branch in s["branches"]][:3]
        stories(selected)
        if branch != "Company" and selected:
            for path in dict.fromkeys(path for s in selected for path in s["article"]["exposure_paths"]
                                      if path.startswith(branch.split()[0] + " ·")):
                story.append(para(path, 9, MUTED))
    governance = [s for s in report["sources"] if s.get("governance")]
    if governance or report.get("rosters"):
        story.append(heading("Leadership and major ownership leads"))
        if governance:
            stories(governance)
        for line, link in roster_lines(report):
            story.append(para(line, 9, INK, 4, link))
    story.extend([heading("Evidence by branch"), grid(COVERAGE, evidence_rows(report), [131, 119, 125, 122]),
                  para("A source can appear in several branches.", 8, MUTED)])
    story.extend([heading("Sources and verification notes"),
                  para(f"{len(cited)} cited sources from {len(report['sources'])} relevant in-window results. "
                       "The workbook lists all relevant sources.", 9, MUTED)])
    for ref, s in cited.items():
        a = s["article"]
        v = a.get("verification") or {}
        story.append(para(f"{ref}   {a['publisher'] or 'Publisher unlisted'}   ·   {a['language']}   |   {headline(a, 'title')}",
                          10, INK, 2, v.get("source_url") or a["url"]))
        story.append(para(source_note(s), 9, MUTED, 8))
    if report["errors"]:
        story.append(heading("Search coverage gaps"))
        story.extend(para(error, 9, MUTED) for error in report["errors"])

    def footer(canvas, doc):
        w,h = A4
        canvas.setFillColor(colors.HexColor("#"+TEAL))
        canvas.rect(44, h-27, w-88, 2, fill=1, stroke=0)
        canvas.setFont(font, 8)
        canvas.drawString(44, 24, f"RA Credit Monitor   /   Run {report['run']['id']}")
        canvas.drawRightString(w-44, 24, str(doc.page))

    def save(buf):
        doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=43, rightMargin=43, topMargin=39, bottomMargin=41,
                                title=subject["name"] + " | Company monitoring report")
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return result_buffer(save)
