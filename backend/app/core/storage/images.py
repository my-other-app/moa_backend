"""An uploaded picture, checked and sized for where it will be shown.

A JPEG, PNG, WebP or GIF is kept in its format; a HEIC, AVIF, BMP or TIFF
(formats.CONVERTED) is stored as a JPEG, or a PNG when some of it is
see-through. Kept or made, no stored picture carries its metadata (EXIF with
a phone's GPS position, XMP, IPTC, comments): one kept as it came has it
taken out losslessly, colour profile kept (storage.metadata, `kept_bytes`).
"""
import io
from uuid import uuid4

from PIL import Image, ImageOps

from app.core.storage import formats, objects
from app.core.storage.metadata import NO_METADATA, keep_colour, without_metadata
from app.response import CustomHTTPException

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_EXT = {"PNG": "png", "JPEG": "jpg", "MPO": "jpg", "WEBP": "webp", "GIF": "gif"}


def checked_image(raw: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> Image.Image:
    """The picture in `raw`, opened, counted and upright -- or the 413/422
    that says why not. The bytes are counted before anything decodes them
    and the pixels from the header before the decoder runs."""
    if len(raw) > max_bytes:
        raise CustomHTTPException(413, message=f"That image is over {max_bytes // (1024 * 1024)} MB. Export it smaller and try again.")
    try:
        image = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError:
        raise CustomHTTPException(422, message=f"That image is too large. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    except Exception:
        raise formats.not_readable(raw)
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise CustomHTTPException(422, message=f"That image is {image.width} x {image.height} pixels. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    try:
        image.load()
    except Exception:
        raise formats.not_readable(raw)
    return image


def encoded(image: Image.Image, fmt: str, *, quality: int = 88) -> tuple[bytes, str]:
    """`image` encoded anew as a JPEG (for "JPEG"), a WebP (for "WEBP") or
    else a PNG, and the format it is: no EXIF, XMP or text, the colour profile
    kept."""
    out = io.BytesIO()
    icc = keep_colour(image)
    if fmt == "JPEG":
        image.convert("RGB").save(out, "JPEG", quality=quality, **icc, **NO_METADATA)
    elif fmt == "WEBP":
        image.save(out, "WEBP", quality=quality, **icc, **NO_METADATA)
    else:
        image.save(out, "PNG", **NO_METADATA)
        fmt = "PNG"
    return out.getvalue(), fmt


def kept_bytes(raw: bytes, image: Image.Image) -> bytes:
    """An upright upload's own bytes, without their metadata (storage.metadata);
    encoded anew, which carries none, when its structure does not parse."""
    fmt = (image.format or "").upper()
    try:
        return without_metadata(raw, fmt)
    except ValueError:
        return encoded(image, "JPEG" if fmt in ("JPEG", "MPO") else fmt)[0]


def store_image(raw: bytes, key_prefix: str, *, max_width: int, name: str = "image") -> dict:
    """Validate, shrink to `max_width`, store under the prefix; answer with
    the key a design saves and a URL to show it now.

    Blocking (a decode, a resize, a PUT): async code runs it with asyncio.to_thread."""
    image = checked_image(raw)
    fmt = (image.format or "").upper()
    convert = fmt in formats.CONVERTED
    if fmt not in _EXT and not convert:
        raise formats.refused(fmt)
    try:
        rotate = (image.getexif().get(0x0112, 1) or 1) != 1
    except Exception:
        rotate = False
    try:
        upright = ImageOps.exif_transpose(image) if rotate else image
        if convert:
            upright, kept = formats.kept_as(upright)
            fmt = "PNG" if kept == "png" else "JPEG"
    except Exception:
        raise formats.not_readable(raw)
    if upright.width > max_width or rotate or convert:
        # Shrunk, turned the way the camera held it, or converted: new bytes,
        # which carry no metadata.
        if upright.width > max_width:
            upright = upright.resize((max_width, round(upright.height * max_width / upright.width)), Image.Resampling.LANCZOS)
        raw, fmt = encoded(upright, "JPEG" if _EXT[fmt] == "jpg" else fmt)
    else:
        # As it came, without its metadata.
        raw = kept_bytes(raw, image)
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
