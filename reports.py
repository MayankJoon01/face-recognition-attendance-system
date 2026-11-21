"""PDF generation with ReportLab."""
from datetime import datetime
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def _table(headers, rows, highlight=()):
    t = Table([headers] + rows, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563eb")),
             ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
             ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
             ("FONTSIZE", (0, 0), (-1, -1), 9),
             ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f5f9")]),
             ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    for i in highlight:
        style.append(("BACKGROUND", (0, i + 1), (-1, i + 1), colors.HexColor("#fee2e2")))
    t.setStyle(TableStyle(style))
    return t


def build_pdf(title, info_lines, sections):
    """sections: list of (heading, headers, rows, highlight_row_indexes)."""
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm, title=title)
    st = getSampleStyleSheet()
    story = [Paragraph(title, st["Title"])]
    for line in info_lines:
        story.append(Paragraph(line, st["Normal"]))
    story.append(Paragraph(f"Generated: {datetime.now():%Y-%m-%d %H:%M}", st["Normal"]))
    for heading, headers, rows, highlight in sections:
        story += [Spacer(1, 8 * mm), Paragraph(heading, st["Heading2"])]
        story.append(_table(headers, rows, highlight) if rows
                     else Paragraph("No records.", st["Italic"]))
    doc.build(story)
    return buf.getvalue()
