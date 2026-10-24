"""An uploaded picture, checked and sized for where it will be shown."""
import io
from uuid import uuid4

from PIL import Image, ImageOps

from app.core.storage import objects
from app.response import CustomHTTPException

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_EXT = {"PNG": "png", "JPEG": "jpg", "MPO": "jpg", "WEBP": "webp"}


def checked_image(raw: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> Image.Image:
    """The picture in `raw`, opened, counted and upright -- or the 413/422
    that says why not. The bytes are counted before anything decodes them
    and the pixels from the header before the decoder runs."""
    if len(raw) > max_bytes:
        raise CustomHTTPException(413, message=f"That image is over {max_bytes // (1024 * 1024)} MB. Export it smaller and try again.")
    try:
        image = Image.open(io.BytesIO(raw))
    except Exception:
        raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise CustomHTTPException(422, message=f"That image is {image.width} x {image.height} pixels. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    try:
        image.load()
    except Exception:
        raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
    return image


def store_image(raw: bytes, key_prefix: str, *, max_width: int, name: str = "image") -> dict:
    """Validate, shrink to `max_width`, store under the prefix; answer with
    the key a design saves and a URL to show it now.

    Blocking (a decode, a resize, a PUT): async code runs it with asyncio.to_thread."""
    image = checked_image(raw)
    fmt = (image.format or "").upper()
    if fmt not in _EXT:
        raise CustomHTTPException(422, message="Use PNG, JPEG or WebP.")
    try:
        rotate = (image.getexif().get(0x0112, 1) or 1) != 1
    except Exception:
        rotate = False
    upright = ImageOps.exif_transpose(image) if rotate else image
    if upright.width > max_width or rotate:
        # Shrunk, or turned the way the camera held it: either way new bytes.
        if upright.width > max_width:
            upright = upright.resize((max_width, round(upright.height * max_width / upright.width)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if _EXT[fmt] == "jpg":
            upright.convert("RGB").save(out, "JPEG", quality=88)
            fmt = "JPEG"
        elif fmt == "WEBP":
            upright.save(out, "WEBP", quality=88)
        else:
            upright.save(out, "PNG")
            fmt = "PNG"
        raw = out.getvalue()
    ext = _EXT[fmt]
    key = f"{key_prefix}{name}-{uuid4().hex}.{ext}"
    objects.put_bytes(key, raw, "image/jpeg" if ext == "jpg" else f"image/{ext}")
    return {"key": key, "url": objects.presigned_url(key), "width": upright.width, "height": upright.height}


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
