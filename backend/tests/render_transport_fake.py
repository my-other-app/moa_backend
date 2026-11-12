"""A stand-in for the render service (moa-render, Gotenberg's Chromium) at
the HTTP level: an httpx transport for app.core.doc_designs.render.

`install(monkeypatch)` puts it in place and returns it. Everything above
the wire is the real code -- P's builder (app.core.layout_v2), A's render
client with its form, its two tries and its RenderUnavailable, our wiring
-- so a test sees the page Chromium would be sent. It answers each
conversion with a real PDF: one page per `<div class="moa-page">` of the
paper the form asks for, whose text is that page's visible words (so
app.core.layout.renderer.pdf_text and pdf_to_png work on it).

`down = True` answers 503 to every call, as the service does when its
queue is full or Chromium is restarting (two of those: RenderUnavailable).
"""
from __future__ import annotations

import html as _html
import io
import re
from dataclasses import dataclass, field

import httpx

_DROP = re.compile(r"<(style|script)\b[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_FIELD = re.compile(rb'name="([A-Za-z]+)"\r\n\r\n([^\r]*)\r\n')


@dataclass
class Call:
    html: str
    form: dict
    pages: int


@dataclass
class FakeRenderService:
    down: bool = False
    calls: list = field(default_factory=list)

    @property
    def renders(self) -> int:
        return len(self.calls)

    def last_html(self) -> str:
        return self.calls[-1].html

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/health"):
            return httpx.Response(200, json={"status": "up", "details": {"chromium": {"status": "up"}}})
        if self.down:
            return httpx.Response(503, text="Service Unavailable")
        body = request.read()
        form = {k.decode(): v.decode() for k, v in _FIELD.findall(body)}
        start = body.find(b"<!doctype html>")
        page_html = body[start:body.rfind(b"</html>") + 7].decode("utf-8") if start >= 0 else ""
        texts = page_texts(page_html)
        self.calls.append(Call(page_html, form, len(texts)))
        return httpx.Response(200, content=pdf_of(texts, form), headers={"content-type": "application/pdf"})


def page_texts(page_html: str) -> list[list[str]]:
    """Each page's visible lines: the text of its layers, in order."""
    body = _DROP.sub("", page_html)
    chunks = body.split('<div class="moa-page"')[1:] or [body]
    out = []
    for chunk in chunks:
        # The rest of the page's own tag (its data-canvas) is not text.
        chunk = chunk.split(">", 1)[1] if ">" in chunk else chunk
        lines = []
        for piece in re.split(r"</div>|<br\s*/?>", chunk):
            text = " ".join(_html.unescape(_TAG.sub(" ", piece)).split())
            if text:
                lines.append(text)
        out.append(lines)
    return out


def _mm(value: str, default: float) -> float:
    try:
        return float(str(value).replace("mm", "")) * 72 / 25.4
    except ValueError:
        return default


def pdf_of(pages: list[list[str]], form: dict) -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas

    w, h = _mm(form.get("paperWidth", "100mm"), 283), _mm(form.get("paperHeight", "100mm"), 283)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(w, h))
    for lines in pages or [[]]:
        c.setFont("Helvetica", 6)
        y = h - 10
        for line in lines:
            c.drawString(4, y, line[:160])
            y -= 7
        c.showPage()
    c.save()
    return buf.getvalue()


def install(monkeypatch) -> FakeRenderService:
    from app.core.doc_designs import render

    fake = FakeRenderService()
    monkeypatch.setattr(render, "_transport", httpx.MockTransport(fake.handler))
    return fake
