"""The closeout report as a PDF: one page or two, the figures the organiser
quotes afterwards, drawn with reportlab's flowables so it never needs a
browser."""
import io
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.layout.tokens import format_date, format_event_time

INK = colors.HexColor("#141614")
INK2 = colors.HexColor("#5A5F5B")
LINE = colors.HexColor("#E1E1DC")


def _styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", parent=base["Title"], fontName="Helvetica-Bold", fontSize=20, leading=24, alignment=0, textColor=INK, spaceAfter=2),
        "meta": ParagraphStyle("meta", parent=base["Normal"], fontName="Helvetica", fontSize=9.5, leading=13, textColor=INK2),
        "section": ParagraphStyle("section", parent=base["Normal"], fontName="Courier-Bold", fontSize=8.5, leading=11, textColor=INK2, spaceBefore=12, spaceAfter=4),
        "big": ParagraphStyle("big", parent=base["Normal"], fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK),
        "label": ParagraphStyle("label", parent=base["Normal"], fontName="Helvetica", fontSize=8.5, leading=11, textColor=INK2),
        "foot": ParagraphStyle("foot", parent=base["Normal"], fontName="Courier", fontSize=8, leading=10, textColor=INK2),
    }


def _table(rows: list, widths: list, header: bool = True) -> Table:
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 0),
    ]
    if header:
        style += [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("TEXTCOLOR", (0, 0), (-1, 0), INK2), ("FONTSIZE", (0, 0), (-1, 0), 8.5)]
    t.setStyle(TableStyle(style))
    return t


def report_pdf(f: dict, generated_at: datetime, tz: str) -> bytes:
    """The figures as a document: attendance first, then where guests came
    in, what they took, what was sent, and what they said afterwards."""
    s = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm, title=f"{f['event_name']} — closeout report", author="My Other App")
    W = A4[0] - 40 * mm
    out = []
    out.append(Paragraph("CLOSEOUT REPORT", s["section"]))
    out.append(Paragraph(f["event_name"], s["title"]))
    when = f"{format_date(datetime.fromisoformat(f['starts_at']), tz)} · {format_event_time(datetime.fromisoformat(f['starts_at']), tz)} – {format_event_time(datetime.fromisoformat(f['ends_at']), tz)}"
    out.append(Paragraph(f"{f.get('org_name') or ''}{' · ' if f.get('org_name') else ''}{when}{' · ' + f['venue'] if f.get('venue') else ''}", s["meta"]))
    out.append(Spacer(1, 10))

    tiles = [
        [Paragraph(str(f["registered"]), s["big"]), Paragraph(str(f["checked_in"]), s["big"]), Paragraph(f"{f['turnout_pct']}%", s["big"]), Paragraph(str(f["no_shows"]), s["big"])],
        [Paragraph("Registered", s["label"]), Paragraph("Checked in", s["label"]), Paragraph("Turnout", s["label"]), Paragraph("No-shows", s["label"])],
    ]
    t = Table(tiles, colWidths=[W / 4] * 4, hAlign="LEFT")
    t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, 0), 0), ("TOPPADDING", (0, 1), (-1, 1), 0)]))
    out.append(t)

    req = f.get("requests")
    if req:
        # Confirm each registration (app/api/approvals): who asked, and what became of it.
        out.append(Paragraph("REGISTRATION REQUESTS", s["section"]))
        rows = [
            ["Asked", str(req.get("total", 0))],
            ["Confirmed", str(req.get("approved", 0))],
            ["  of them paid", str(req.get("approved_paid", 0))],
            ["  checked in", str(req.get("checked_in", 0))],
            ["Declined", str(req.get("declined", 0))],
            ["Withdrew", str(req.get("withdrawn", 0))],
            ["Didn't pay in time", str(req.get("lapsed", 0))],
            ["Closed unanswered or settled", str(req.get("closed", 0))],
            ["Added directly (desk, import, before the switch)", str(req.get("added_directly", 0))],
        ]
        if req.get("avg_decision_hours") is not None:
            rows.append(["Average time to decide", f"{req['avg_decision_hours']} h"])
        out.append(_table(rows, [W * 0.8, W * 0.2], header=False))

    out.append(Paragraph("BY ROLE", s["section"]))
    out.append(_table([["Role", "Registered", "Checked in"]] + [[r["label"], str(r["registered"]), str(r["checked_in"])] for r in f["by_role"]], [W * 0.6, W * 0.2, W * 0.2]))

    out.append(Paragraph("HOW THEY CAME IN", s["section"]))
    rows = [["Way in", "Guests"]] + [[v["label"], str(v["count"])] for v in f["by_via"]]
    rows.append(["Walk-ups registered at the door", str(f["walk_ins"])])
    rows.append(["Phone numbers on record", str(f["with_phone"])])
    out.append(_table(rows, [W * 0.8, W * 0.2]))

    if f.get("merch"):
        out.append(Paragraph("MERCHANDISE", s["section"]))
        out.append(_table([["Item", "Handed out", "Of"]] + [[m["name"], str(m["collected"]), str(m["total"])] for m in f["merch"]], [W * 0.6, W * 0.2, W * 0.2]))

    if f.get("messages"):
        out.append(Paragraph("MESSAGES", s["section"]))
        out.append(_table([["Subject", "Audience", "Sent", "Failed", "Skipped"]] + [[m["subject"] or "—", m["audience"], str(m["sent"]), str(m["failed"]), str(m["skipped"])] for m in f["messages"]], [W * 0.44, W * 0.2, W * 0.12, W * 0.12, W * 0.12]))

    out.append(Paragraph("AFTERWARDS", s["section"]))
    rows = [["Certificates issued", str(f["certificates_issued"])], ["ID cards printed", str(f["cards_printed"])]]
    rows.append(["Ratings", f"{f['rating_avg']:.1f} from {f['rating_count']}" if f["rating_count"] else "none yet"])
    out.append(_table(rows, [W * 0.8, W * 0.2], header=False))

    out.append(Spacer(1, 16))
    out.append(Paragraph(f"Generated {format_date(generated_at, tz)} {format_event_time(generated_at, tz)} · My Other App", s["foot"]))
    doc.build(out)
    return buf.getvalue()
