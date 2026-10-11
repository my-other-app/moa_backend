"""An uploaded picture, checked and sized for where it will be shown."""
import io
from uuid import uuid4

from PIL import Image

from app.core.storage import objects
from app.response import CustomHTTPException

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_EXT = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}


def store_image(raw: bytes, key_prefix: str, *, max_width: int, name: str = "image") -> dict:
    """Validate, shrink to `max_width`, store under the prefix; answer with
    the key a design saves and a URL to show it now."""
    if len(raw) > MAX_IMAGE_BYTES:
        raise CustomHTTPException(413, message="That image is over 5 MB. Export it smaller and try again.")
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception:
        raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
    fmt = (image.format or "").upper()
    if fmt not in _EXT:
        raise CustomHTTPException(422, message="Use PNG, JPEG or WebP.")
    if image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if fmt == "JPEG":
            image.convert("RGB").save(out, "JPEG", quality=88)
        else:
            image.save(out, "PNG")
            fmt = "PNG"
        raw = out.getvalue()
    ext = _EXT[fmt]
    key = f"{key_prefix}{name}-{uuid4().hex}.{ext}"
    objects.put_bytes(key, raw, "image/jpeg" if ext == "jpg" else f"image/{ext}")
    return {"key": key, "url": objects.presigned_url(key), "width": image.width, "height": image.height}


def image_ext(data: bytes) -> str:
    """The file extension the bytes say they are, for a MIME part."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"GIF8":
        return "gif"
    return "png"
