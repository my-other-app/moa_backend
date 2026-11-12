"""The ticket, certificate and ID card designs (docs: feature-register
doc-designs-contract.md; this package's CHANGES.md and templates/).

The interface the wiring codes against:

    catalogue(kind) -> list[DesignInfo]      kind: "ticket" | "certificate" | "badge"
    get(design_id) -> DesignInfo             design_id: "certificate/07-swiss-minimal"
    validate(design_id, options, bindings, accent=None) -> list[problem]
    fill(design_id, values, options, bindings, accent) -> str        (the page, fonts inline)
    preview_html(design_id, values, options, bindings, accent, base_url=None) -> str
    fingerprint(design_id, values, options, bindings, accent) -> str (D8)
    await render_pdf(page_or_pages, page=None) -> bytes               (D1, D9)
    await render_health() -> dict

    DEFAULTS[kind] -> design id (D7)
"""
from app.core.doc_designs.designs import KINDS, AccentInfo, DesignInfo, PageSpec, catalogue, get
from app.core.doc_designs.errors import BadValue, DesignError, LeftoverToken, MissingValue, UnknownDesign
from app.core.doc_designs.fill import contrast, fill, fingerprint, preview_html, validate
from app.core.doc_designs.render import RenderError, RenderUnavailable, combine, render_pdf
from app.core.doc_designs.render import health as render_health

#: The designs used when nothing is saved (contract, D7).
DEFAULTS = {
    "ticket": "ticket/03-daylight-boarding-pass",
    "certificate": "certificate/07-swiss-minimal",
    "badge": "badge/06-thermal-mono",
}

__all__ = [
    "KINDS", "DEFAULTS", "AccentInfo", "DesignInfo", "PageSpec", "catalogue", "get", "validate", "fill",
    "preview_html", "fingerprint", "render_pdf", "render_health", "combine", "contrast",
    "DesignError", "UnknownDesign", "MissingValue", "BadValue", "LeftoverToken", "RenderError", "RenderUnavailable",
]
