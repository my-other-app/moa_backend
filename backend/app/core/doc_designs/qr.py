"""QR codes for the designs: black on white, as an SVG data: URI (contract, D4 c).

One path of whole modules, `crispEdges`, a white square behind it: sharp at
any print size, a few hundred bytes, and nothing a renderer has to fetch.
"""
from __future__ import annotations

import base64
from functools import lru_cache

import qrcode
from qrcode.constants import ERROR_CORRECT_H, ERROR_CORRECT_L, ERROR_CORRECT_M, ERROR_CORRECT_Q

_ECC = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}


def svg(text: str, ecc: str = "M", border: int = 0) -> str:
    """The QR code for `text` as an SVG document."""
    qr = qrcode.QRCode(error_correction=_ECC[ecc], border=border, box_size=1)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    runs = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                runs.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
            else:
                x += 1
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
        f'<rect width="{n}" height="{n}" fill="#fff"/><path fill="#000" d="{"".join(runs)}"/></svg>'
    )


@lru_cache(maxsize=512)
def data_uri(text: str, ecc: str = "M", border: int = 0) -> str:
    """`svg(...)` as a data: URI, for an <img src>."""
    return "data:image/svg+xml;base64," + base64.b64encode(svg(text, ecc, border).encode()).decode()
