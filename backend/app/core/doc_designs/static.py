"""/static/doc-fonts/* and /static/doc-designs/*: the designs' fonts and
preview images for browsers (contract, D3, D4).

The editor's live preview is a sandboxed srcdoc iframe on the dashboard's
origin (an opaque one, in fact), so these are cross-origin font loads: they
need `Access-Control-Allow-Origin: *` and a resource policy that lets another
origin use them. Every file name carries its content's hash and fonts.css is
linked with ?v=<lock digest> (fonts.css_url), so all of it may be cached for a
year, immutable. Only files in the lock are served (the WOFF2 files, the
licences, the lock itself and fonts.css): no path reaches anything else.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from app.core.doc_designs import designs, fonts

router = APIRouter()

TYPES = {
    ".woff2": "font/woff2",
    ".css": "text/css; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}
HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
    "Access-Control-Allow-Origin": "*",
    "Cross-Origin-Resource-Policy": "cross-origin",
}


@router.api_route(fonts.STATIC_PREFIX + "/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def doc_font(path: str):
    file = fonts.resolve(path)
    if file is None or not file.is_file():
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return FileResponse(file, media_type=TYPES.get(file.suffix, "application/octet-stream"), headers=HEADERS)


@router.api_route(designs.PREVIEW_PREFIX + "/{kind}/{name}.png", methods=["GET", "HEAD"], include_in_schema=False)
async def design_preview(kind: str, name: str):
    """A design's preview image (the designer's PNG; tickets: our render).
    Linked as DesignInfo.preview_url, with ?v=<design digest>."""
    try:
        d = designs.get(f"{kind}/{name}")
    except Exception:  # noqa: BLE001 - an unknown design is a 404
        d = None
    if d is None or d.preview is None or not d.preview.is_file():
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    return FileResponse(d.preview, media_type="image/png", headers=HEADERS)
