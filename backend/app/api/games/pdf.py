"""The hunt's QR sheet: one code per page, with its place and number, never the clue."""

from __future__ import annotations

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


def _matrix(text: str) -> list:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=0, box_size=1)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.get_matrix()


def points_pdf(event_name: str, points: list) -> bytes:
    """`points`: (number, label, token) in order; one A4 page each."""
    out = io.BytesIO()
    page_w, page_h = A4
    pdf = canvas.Canvas(out, pagesize=A4, pageCompression=1)
    pdf.setTitle(f"{event_name}: hunt QR codes"[:100])
    for number, label, token in points:
        matrix = _matrix(token)
        n = len(matrix)
        size = 420.0
        cell = size / (n + 8)  # a four-module quiet zone all round
        x0, y0 = (page_w - size) / 2, page_h - 150 - size
        pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(x0, y0, size, size, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)
        for r, row in enumerate(matrix):
            for c, on in enumerate(row):
                if on:
                    pdf.rect(x0 + (c + 4) * cell, y0 + size - (r + 5) * cell, cell + 0.2, cell + 0.2, stroke=0, fill=1)
        pdf.setFont("Helvetica-Bold", 64)
        pdf.drawCentredString(page_w / 2, page_h - 100, f"#{number}")
        pdf.setFont("Helvetica", 26)
        pdf.drawCentredString(page_w / 2, y0 - 50, label[:60])
        pdf.setFont("Helvetica", 11)
        pdf.drawCentredString(page_w / 2, 40, event_name[:80])
        pdf.showPage()
    pdf.save()
    return out.getvalue()
