"""The pictures a ticket or certificate design names: kept as keys, shown as
fresh links.

A design's config used to keep `logo_url`, `background_image_url` and the
rest as the presigned URL the upload produced -- valid for one hour. The
HTML templates printed straight from those URLs, so about an hour after a
design was saved its logo, artwork and signatures vanished from every ticket
and certificate, and from the designer.

The config now keeps the key (`logo_key`, ...). A link is signed from it
whenever the config is shown, and a render fetches the bytes by key and
embeds them, so nothing a document needs can expire. A config saved before
this still names its keys inside its old URLs; reading it reads them out
(`key_of`), and the next save stores them as keys.

A key in a config is never trusted as given -- the config comes from the
dashboard -- so only keys inside the template image folders are signed or
read. Anything else would let a design sign a link to any object in the
bucket.
"""
from __future__ import annotations

import base64
import re
from typing import Iterable, Optional

from app.core.storage import objects
from app.core.storage.bucket import key_from_url
from app.core.storage.fields import signed_url, stored_key
from app.core.storage.images import image_ext

TICKET_FIELDS = ("logo", "secondary_logo", "background_image")
CERTIFICATE_FIELDS = (*TICKET_FIELDS, "signature_1", "signature_2")
ALL_FIELDS = CERTIFICATE_FIELDS

# Where the template image columns upload (S3ImageField upload_to). Matched
# anywhere after the base path, so a key stored under an earlier base path
# still reads.
_FOLDER = re.compile(r"(^|/)templates/(tickets|certificates)/(logos|backgrounds|signatures)/[^/]+$")


def allowed(key: Optional[str]) -> bool:
    return bool(key) and isinstance(key, str) and ".." not in key.split("/") and bool(_FOLDER.search(key))


def key_of(config: dict, field: str, template=None) -> Optional[str]:
    """The key the design holds for `field`.

    The config's own `{field}_key` when it has one (even None: removed);
    else the key inside a legacy `{field}_url`; else, for a config that
    never named the field at all, the template row's column.
    """
    config = config or {}
    if f"{field}_key" in config:
        key = config.get(f"{field}_key")
        return key if allowed(key) else None
    if f"{field}_url" in config:
        key = key_from_url(config.get(f"{field}_url"))
        return key if allowed(key) else None
    if template is not None:
        key = stored_key(getattr(template, field, None))
        return key if allowed(key) else None
    return None


def stored(config: dict, template=None, *, fields: Iterable[str] = ALL_FIELDS, touched: Iterable[str] = ()) -> dict:
    """The config as it is saved: a key for every image field, no URLs.

    A field in `touched` was uploaded or removed in this request, so the
    template's column says what it is now; every other field keeps what the
    config already named (a fork keeps its signatures, which have no column
    on the copy).
    """
    out = dict(config or {})
    touched = set(touched)
    for field in fields:
        if field in touched and template is not None:
            key = stored_key(getattr(template, field, None))
            key = key if allowed(key) else None
        else:
            key = key_of(out, field, template)
        out[f"{field}_key"] = key
        out.pop(f"{field}_url", None)
    return out


def shown(config: dict, template=None, *, fields: Iterable[str] = ALL_FIELDS) -> dict:
    """The config as it is handed out: each key, and a link signed now."""
    out = dict(config or {})
    for field in fields:
        key = key_of(out, field, template)
        out[f"{field}_key"] = key
        out[f"{field}_url"] = signed_url(key) if key else None
    return out


def data_uri(data: bytes) -> str:
    ext = image_ext(data)
    mime = "image/jpeg" if ext == "jpg" else f"image/{ext}"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def embedded(style: dict, *, fields: Iterable[str] = ALL_FIELDS) -> tuple[dict, bool]:
    """The style for an HTML render, every image in it as a data: URI.

    wkhtmltopdf then fetches nothing: no remote request to hang on, nothing
    to expire. Bytes come from the per-process image cache (objects).
    Returns the style and whether every image it names was read -- a render
    missing one is drawn, but not kept (app.api.events.doc_cache).
    """
    out = dict(style or {})
    complete = True
    for field in fields:
        key = key_of(out, field)
        url = None
        if key:
            data = objects.get_bytes(key)
            if data:
                url = data_uri(data)
            else:
                complete = False
        out[f"{field}_url"] = url
    return out, complete


def image_keys(style: dict, *, fields: Iterable[str] = ALL_FIELDS) -> dict:
    """field -> key, for the ones the style names."""
    return {f: k for f in fields if (k := key_of(style or {}, f))}
