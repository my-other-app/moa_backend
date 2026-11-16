"""Cookies the API sets: whether they are Secure.

In production (DEBUG off) always: the browser only ever reaches the API and
the dashboards over https, and a proxy that stopped passing on the scheme
must not quietly drop the flag. Anywhere else, as the request came: a local
or staging server on http://localhost still gets its cookies back.
"""

from __future__ import annotations

from app.config import settings


def secure(request=None) -> bool:
    from app.core.config_checks import is_production

    if not settings.DEBUG and is_production(settings):
        return True
    return request is not None and request.url.scheme == "https"
