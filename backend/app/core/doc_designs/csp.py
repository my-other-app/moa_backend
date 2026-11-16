"""The Content-Security-Policy every page sent to the render service carries.

A page is printed by Chromium with its fonts and pictures inline (data:
URIs) and one script of ours, the fit script. The policy says exactly that:
nothing may be loaded but data: images and fonts, inline styles, and that
one script by its hash; no other script runs, nothing is fetched, framed or
submitted, whatever ended up in the page. Pages for a browser (an editor's
preview, which loads its fonts from the API) do not carry it.
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache


@lru_cache(maxsize=8)
def policy(script: str) -> str:
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode(
        "ascii"
    )
    return (
        "default-src 'none'; img-src data:; font-src data:; style-src 'unsafe-inline'; "
        f"script-src 'sha256-{digest}'; base-uri 'none'; form-action 'none'"
    )


def meta(script: str) -> str:
    """The <meta> for a page whose only script is `script` (as embedded)."""
    return f'<meta http-equiv="Content-Security-Policy" content="{policy(script)}">\n'
