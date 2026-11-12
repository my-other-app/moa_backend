"""STUB -- Backend B's stand-in for Backend A's package. DROP AT MERGE.

Backend A builds the real `app.core.doc_designs` (the 35 designer templates,
their manifests, the fonts and the client of the moa-render service) on
feat/doc-designs-core. This file only lets feat/doc-designs-wiring import
the interface as A built it (its __init__ docstring) and run without it: it
knows no designs and renders nothing, so every preset render takes the D9
fallback, and the tests put a fake in its place (tests/doc_designs_fake.py).

Everything B calls goes through app/api/doc_presets/engine.py.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Union

KINDS = ("ticket", "certificate", "badge")
DEFAULTS = {
    "ticket": "ticket/03-daylight-boarding-pass",
    "certificate": "certificate/07-swiss-minimal",
    "badge": "badge/06-thermal-mono",
}


class DesignError(ValueError):
    pass


class UnknownDesign(DesignError):
    pass


class MissingValue(DesignError):
    pass


class BadValue(DesignError):
    pass


class LeftoverToken(DesignError):
    pass


class RenderError(RuntimeError):
    pass


class RenderUnavailable(RenderError):
    pass


def catalogue(kind: str) -> list:
    return []


def get(design_id: str):
    raise UnknownDesign(f"no such design: {design_id}")


def validate(design_id: str, options: Optional[Mapping] = None, bindings: Optional[Mapping] = None, accent: Optional[str] = None) -> list[dict]:
    return [{"code": "unknown_design", "field": "id", "message": f"no such design: {design_id}", "level": "error"}]


def fill(design_id: str, values: Mapping[str, Any], options=None, bindings=None, accent=None) -> str:
    raise UnknownDesign(f"no such design: {design_id}")


def preview_html(design_id: str, values: Mapping[str, Any], options=None, bindings=None, accent=None, *, base_url=None) -> str:
    raise UnknownDesign(f"no such design: {design_id}")


def fingerprint(design_id: str, values: Mapping[str, Any], options=None, bindings=None, accent=None) -> str:
    raise UnknownDesign(f"no such design: {design_id}")


async def render_pdf(html_or_pages: Union[str, Sequence[str]], page=None, *, timeout=None) -> bytes:
    raise RenderUnavailable("no render service on this branch")


async def render_health(timeout: float = 2.0) -> dict:
    return {"ok": False, "status": "unreachable", "chromium": None, "error": "stub", "ms": 0}
