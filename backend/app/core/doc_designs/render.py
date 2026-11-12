"""PDFs from the render service: Gotenberg's Chromium (contract, D1, D2, D9).

`render_pdf(page_html)` posts one filled page (app.core.doc_designs.fill) to
`APP_RENDER_URL` /forms/chromium/convert/html and returns the PDF. A list of
pages becomes one PDF of that many pages in one conversion -- a badge batch,
a certificate bundle -- with their fonts and styles declared once.

Chromium prints when the page's fit script says `window.__moaReady === true`
(fonts and images loaded, long text fitted), on paper of the design's size,
with no margins and with backgrounds. The page holds everything it shows
(fonts and pictures as data: URIs), so the service needs no network and is
given none (docker-compose.prod.yml; docs/OPERATIONS.md, "The render
service").

**Timeouts (D9).** Each try may take APP_RENDER_TIMEOUT seconds, plus
PER_PAGE for every page after the first (at most MAX_TIMEOUT); connecting
may take CONNECT. A try that cannot connect, times out, or gets a 429/5xx
(the service's queue is full, Chromium restarting) is tried once more. Two
failures raise RenderUnavailable: the caller falls back to the classic
renderer for that request (tickets, certificates) or answers 503 (ID cards),
and raises an admin alert (app.api.admin_alerts.notify, which already keeps
one per ten minutes). Any other answer is our mistake, not an outage:
RenderError.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional, Sequence, Union

import httpx

from app.config import settings
from app.core.doc_designs.designs import PageSpec

logger = logging.getLogger(__name__)

ROUTE = "/forms/chromium/convert/html"
TRIES = 2
CONNECT = 2.0
PER_PAGE = 0.25
MAX_TIMEOUT = 280.0
#: What the page sets once it is ready to print (fit.js).
READY = "window.__moaReady === true"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class RenderError(RuntimeError):
    """The render service refused this page (a bad request: our bug)."""


class RenderUnavailable(RenderError):
    """The render service is down, overloaded or too slow (after TRIES)."""


_HEAD = re.compile(r"<!--moa:head-->(.*?)<!--/moa:head-->", re.S)
_PAGE = re.compile(r"<!--moa:page-->.*?<!--/moa:page-->", re.S)
_SIZE = re.compile(r'<meta name="moa-page" content="([\d.]+mm) ([\d.]+mm)">')
_SCRIPT = re.compile(r'<script data-moa="fit">.*?</script>', re.S)
_STYLE = re.compile(r"<style([^>]*)>(.*?)</style>", re.S)


def combine(pages: Sequence[str]) -> str:
    """Several filled pages as one document: the first page's shell, every
    page's body, and each distinct font rule and style once. The pages must
    share a paper size."""
    if not pages:
        raise ValueError("no pages to render")
    if len(pages) == 1:
        return pages[0]
    sizes = {_SIZE.search(p).groups() for p in pages}
    if len(sizes) != 1:
        raise ValueError(f"pages of different sizes cannot share a PDF: {sorted(sizes)}")
    font_rules: dict[str, None] = {}
    styles: dict[str, None] = {}
    bodies = []
    for p in pages:
        for attrs, css in _STYLE.findall(_HEAD.search(p).group(1)):
            if 'data-moa="fonts"' in attrs:
                for rule in css.strip().splitlines():
                    if rule:
                        font_rules[rule] = None
            else:
                styles[f"<style{attrs}>{css}</style>"] = None
        bodies.append(_PAGE.search(p).group(0))
    first = pages[0]
    head = '\n<style data-moa="fonts">\n' + "\n".join(font_rules) + "\n</style>\n" + "\n".join(styles) + "\n"
    first = _HEAD.sub(lambda _m: f"<!--moa:head-->{head}<!--/moa:head-->", first, count=1)
    return _PAGE.sub(lambda _m: "\n".join(bodies), first, count=1)


def page_of(html: str) -> tuple[str, str]:
    m = _SIZE.search(html)
    if not m:
        raise ValueError("not a filled design page (no moa-page size)")
    return m.group(1), m.group(2)


def form(paper: tuple[str, str]) -> dict[str, str]:
    """Gotenberg's form fields for one conversion."""
    return {
        "paperWidth": paper[0],
        "paperHeight": paper[1],
        "marginTop": "0",
        "marginBottom": "0",
        "marginLeft": "0",
        "marginRight": "0",
        "printBackground": "true",
        "preferCssPageSize": "false",
        "waitForExpression": READY,
        "emulatedMediaType": "print",
        "skipNetworkIdleEvent": "true",
        "failOnConsoleExceptions": "true",
    }


def timeout_for(pages: int) -> float:
    return min(MAX_TIMEOUT, float(settings.RENDER_TIMEOUT) + PER_PAGE * max(0, pages - 1))


#: Tests set a fake service here (an httpx transport).
_transport: Optional[httpx.AsyncBaseTransport] = None


def _client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=_transport)


def _trace() -> dict:
    try:
        from app.core.observability import current_request_id

        rid = current_request_id()
    except Exception:  # noqa: BLE001 - a trace id is a nicety
        rid = None
    return {"Gotenberg-Trace": rid} if rid else {}


async def render_pdf(html_or_pages: Union[str, Sequence[str]], page: Optional[PageSpec] = None, *,
                     timeout: Optional[float] = None) -> bytes:
    """A PDF of one filled page, or of several as one document (one page
    each). `page` defaults to the size the page declares."""
    html = html_or_pages if isinstance(html_or_pages, str) else combine(list(html_or_pages))
    paper = page.paper if page else page_of(html)
    count = html.count('<div class="moa-page"')
    limit = timeout or timeout_for(count)
    url = settings.RENDER_URL.rstrip("/") + ROUTE
    body = html.encode("utf-8")
    last: Optional[str] = None
    for attempt in range(1, TRIES + 1):
        started = time.monotonic()
        try:
            async with _client(httpx.Timeout(limit, connect=CONNECT)) as client:
                r = await client.post(url, data=form(paper), files={"files": ("index.html", body, "text/html")},
                                      headers=_trace())
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if r.status_code == 200 and r.content[:5] == b"%PDF-":
                logger.info("rendered %d page(s) in %.0f ms", count, (time.monotonic() - started) * 1000)
                return r.content
            if r.status_code not in RETRY_STATUS:
                raise RenderError(f"render service answered {r.status_code}: {r.text[:300]}")
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        logger.warning("render try %d of %d failed: %s", attempt, TRIES, last)
    raise RenderUnavailable(f"render service unavailable after {TRIES} tries: {last}")


async def health(timeout: float = 2.0) -> dict:
    """The render service's own health (Gotenberg's /health: Chromium up?),
    for the admin overview. Never raises."""
    started = time.monotonic()
    try:
        async with _client(httpx.Timeout(timeout)) as client:
            r = await client.get(settings.RENDER_URL.rstrip("/") + "/health")
        ms = round((time.monotonic() - started) * 1000)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        chromium = ((data.get("details") or {}).get("chromium") or {}).get("status")
        return {"ok": r.status_code == 200 and data.get("status") == "up", "status": data.get("status"),
                "chromium": chromium, "ms": ms}
    except Exception as exc:  # noqa: BLE001 - a health probe reports, it does not fail
        return {"ok": False, "status": "unreachable", "chromium": None, "error": type(exc).__name__,
                "ms": round((time.monotonic() - started) * 1000)}
