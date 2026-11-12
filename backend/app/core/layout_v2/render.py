"""PDFs of v2 layouts, through the render service (doc-designs D1, D2, D9).

The page `build_html` makes is the same shell app.core.doc_designs fills
(its head and page markers, its fit script), so the render client is
doc_designs.render_pdf, unchanged: its timeouts, its two tries, its
RenderUnavailable when the service is down, and `combine` for a batch
(one conversion, one page each, every font rule once).
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence, Union

from app.core import doc_designs
from app.core.layout_v2.build import build_html
from app.core.layout_v2.schema import LayoutV2, parse


async def render_pdf(layout: Union[LayoutV2, dict], values: Mapping[str, Any], context=None, *,
                     kind: str = "ticket", skip_preprinted: bool = False, timeout: Optional[float] = None) -> bytes:
    """One page's PDF, on paper of the layout's page size."""
    html = build_html(layout, values, context=context, kind=kind, skip_preprinted=skip_preprinted)
    return await doc_designs.render_pdf(html, timeout=timeout)


async def render_pages(pages: Iterable[Sequence], *, kind: str = "ticket", skip_preprinted: bool = False,
                       timeout: Optional[float] = None) -> bytes:
    """Several pages as one PDF: each item is (layout, values, context). Every
    page must have the same paper size (doc_designs.combine)."""
    html = [build_html(parse(p[0]), p[1], context=p[2] if len(p) > 2 else None, kind=kind,
                       skip_preprinted=skip_preprinted) for p in pages]
    return await doc_designs.render_pdf(html, timeout=timeout)
