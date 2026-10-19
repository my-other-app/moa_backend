"""Pictures that get printed: made ready for print once, when they arrive.

A ticket's, certificate's or ID card's artwork, and the logos, signatures
and pictures placed on it, are drawn into every PDF made from the design.
They were stored as uploaded, and what a PDF costs follows the picture
(docs/stage0/AUDIT.md, 2.3): with no artwork a ticket draws in 7-12 ms and
weighs 38-68 KB; over a 54 KB JPEG, about 30 ms and 130 KB; over a 1.36 MB
one, about 270 ms and 1.76 MB, for every guest. The renderer copies a JPEG
into the PDF as it is, and decodes anything else and compresses its pixels
again, so the stored file is what each PDF carries.

`normalise` is what every upload of such a picture goes through now
(app/api/event_designs/router.py `_store_layout_image` for the designer's
templates and pictures, club's and event's; the template image columns, see
fields.S3ImageField `print_art`), and scripts/optimize_doc_art.py does the
same to what was stored before:

- **Upright first.** The camera's orientation (EXIF) is applied: the
  renderers ignore it, so a phone photo printed on its side.
- **Sized for print.** The longest side is at most APP_DOC_ART_MAX_PX
  (2400): an A4 certificate at about 200 dpi, a ticket at more. A design
  is drawn at its canvas's size whatever the picture's pixels, so a smaller
  file prints the same. A very long, thin picture keeps its short side at
  MIN_SIDE at least (the designer's canvas is never narrower than that).
- **Colour in sRGB, no metadata.** A picture with its own colour profile
  (a phone's Display P3, an Adobe RGB export) is converted to sRGB, which is
  what an untagged picture is read as; then EXIF, the profile, text chunks
  and the rest are left behind.
- **A photo becomes a JPEG**, quality JPEG_QUALITY (85), progressive and
  optimised: what the renderer embeds without decoding.
- **A picture with transparency stays a PNG**, compressed losslessly. One
  whose alpha is opaque everywhere has none, and is treated as a photo.
- **Quantised only when that loses nothing.** A PNG goes to a 256-colour
  palette only when converting back gives every pixel exactly as it was (a
  flat logo, a signature in one ink, a design with a few colours). A lossy
  palette is never made: it bands gradients and roughens anti-aliased edges,
  which print shows at 200 dpi and more, and the renderer decodes a PNG and
  compresses its pixels again anyway, so a palette saves bucket bytes but
  not PDF bytes. A flat picture without transparency is kept as such a PNG
  when that is no larger than its JPEG, which is lossless and as small.

Refused, as the uploads always were: more than the path's byte limit (413),
anything that is not a PNG, JPEG, WebP or GIF picture, and anything over
MAX_IMAGE_PIXELS (422), counted from the header before anything is decoded,
so a small file claiming to be enormous never is.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageChops, ImageOps

from app.config import settings
from app.response import CustomHTTPException

#: Pixels a picture may have, counted from its header (the same limit as every upload's).
MAX_IMAGE_PIXELS = 40_000_000
#: The designer's upload limit; the image columns pass their own.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
JPEG_QUALITY = 85
#: The shortest side a capped picture keeps (TicketCanvas.width's floor).
MIN_SIDE = 300
#: What arrives: a JPEG (an MPO is a camera's JPEG with a second frame), PNG, WebP or GIF.
ACCEPTED = frozenset({"JPEG", "MPO", "PNG", "WEBP", "GIF"})
_ORIENTATION = 0x0112


@dataclass
class PrintArt:
    """A picture ready to store: its bytes, "JPEG" or "PNG", and its size."""

    data: bytes
    format: str
    width: int
    height: int

    @property
    def content_type(self) -> str:
        return "image/jpeg" if self.format == "JPEG" else "image/png"

    def ext(self, *, jpeg: str = "jpg") -> str:
        """The extension a key ends in: the image columns write "jpeg", the designer "jpg"."""
        return jpeg if self.format == "JPEG" else "png"


def max_side() -> int:
    return max(MIN_SIDE, int(settings.DOC_ART_MAX_PX or 2400))


def _not_readable() -> CustomHTTPException:
    return CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")


def _too_many_pixels(width: int, height: int) -> CustomHTTPException:
    return CustomHTTPException(
        422, message=f"That image is {width} x {height} pixels. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.",
    )


def opened(raw: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> Image.Image:
    """The picture, opened but not decoded, after every check that needs no decode."""
    if max_bytes and len(raw) > max_bytes:
        raise CustomHTTPException(413, message=f"That image is over {max_bytes // (1024 * 1024)} MB. Export it smaller and try again.")
    try:
        image = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError:
        raise CustomHTTPException(422, message=f"That image is too large. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    except Exception:
        raise _not_readable()
    if (image.format or "").upper() not in ACCEPTED:
        raise CustomHTTPException(422, message="Use PNG, JPEG or WebP.")
    if image.width * image.height > MAX_IMAGE_PIXELS:
        raise _too_many_pixels(image.width, image.height)
    return image


def target_size(size: tuple[int, int], longest: Optional[int] = None) -> tuple[int, int]:
    """`size` with its longest side at most `longest`, never enlarged, the
    short side kept at MIN_SIDE or more (and so, for a picture past 8:1,
    the long side past `longest`)."""
    w, h = size
    longest = longest or max_side()
    scale = min(1.0, longest / max(w, h))
    if scale < 1.0 and min(w, h) * scale < MIN_SIDE:
        scale = min(1.0, MIN_SIDE / min(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def _srgb(image: Image.Image) -> Image.Image:
    """The pixels in sRGB, when the picture names another colour space
    (a grey one is left: grey is grey)."""
    icc = image.info.get("icc_profile")
    if not icc or image.mode not in ("RGB", "RGBA", "CMYK"):
        return image
    try:
        from PIL import ImageCms

        source = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        if image.mode != "CMYK" and "srgb" in (ImageCms.getProfileDescription(source) or "").lower():
            return image
        alpha = image.getchannel("A") if image.mode == "RGBA" else None
        out = ImageCms.profileToProfile(
            image.convert("RGB") if alpha is not None else image, source, ImageCms.createProfile("sRGB"),
            renderingIntent=ImageCms.Intent.PERCEPTUAL, outputMode="RGB",
        )
        if alpha is not None:
            out.putalpha(alpha)
        return out
    except Exception:  # noqa: BLE001 - a profile that cannot be read: the pixels as they are
        return image


def _has_alpha(image: Image.Image) -> bool:
    return image.mode in ("RGBA", "LA", "PA", "RGBa", "La") or "transparency" in image.info


def _pixels(image: Image.Image) -> Image.Image:
    """RGB, RGBA or L: what is encoded. Alpha that is opaque everywhere is dropped."""
    if _has_alpha(image):
        image = image.convert("RGBA")
        low, _ = image.getchannel("A").getextrema()
        if low == 255:
            image = image.convert("RGB")
    elif image.mode in ("I;16", "I;16B", "I;16L", "I"):
        # 16-bit grey: scaled to 8 bits, not clipped to white.
        image = image.convert("I").point(lambda v: v * (1 / 257)).convert("L")
    elif image.mode == "1":
        image = image.convert("L")
    elif image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    return image


def _png(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, "PNG", optimize=True)
    return out.getvalue()


def _jpeg(image: Image.Image, *, sharp: bool) -> bytes:
    """Quality JPEG_QUALITY, progressive. `sharp` (a picture that was made,
    not photographed: a PNG, a WebP) keeps its colour at full resolution
    (4:4:4), so coloured type and hairlines keep their edges; a photo's
    colour is halved (4:2:0), which nobody sees in a photo."""
    out = io.BytesIO()
    image.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True, subsampling=0 if sharp else 2)
    return out.getvalue()


def exact_palette(image: Image.Image) -> Optional[Image.Image]:
    """The picture on a palette of at most 256 colours, when that changes
    no pixel at all; else None. A grey picture is its own (8-bit) palette."""
    if image.mode == "L":
        return image
    colours = image.getcolors(256)
    if colours is None:
        return None
    try:
        # As few entries as there are colours: a two-colour signature is a 1-bit PNG.
        method = Image.Quantize.FASTOCTREE if image.mode == "RGBA" else Image.Quantize.MEDIANCUT
        palette = image.quantize(colors=max(1, len(colours)), method=method, dither=Image.Dither.NONE)
    except Exception:  # noqa: BLE001
        return None
    if ImageChops.difference(palette.convert(image.mode), image).getbbox() is not None:
        return None
    return palette


def _fresh(image: Image.Image) -> Image.Image:
    """The same pixels with nothing else: no profile, no text, no EXIF goes
    out (Pillow writes a PNG's profile from `info` unless told otherwise)."""
    image.info = {}
    return image


# ─── The upload's own bytes, without its metadata ───────────────────────────
#
# A picture already at its size, upright and in sRGB may be smallest as it
# came: a JPEG saved at quality 60 grows when saved again at 85 (and loses a
# little more each time), and a PNG through a better compressor than zlib's
# is smaller than Pillow makes it. So its own bytes are a candidate too, with
# every segment that is not the picture taken out: EXIF (where a phone keeps
# the place it was taken), XMP, IPTC, comments, thumbnails, profiles. Kept
# only when they decode to exactly the same pixels.

#: JFIF and Adobe: the second says how a JPEG's colour is to be read.
_JPEG_KEEP_APP = frozenset({0xE0, 0xEE})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = frozenset({b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND"})


def stripped_jpeg(raw: bytes) -> Optional[bytes]:
    """The JPEG without its APPn segments (but JFIF's and Adobe's) and
    comments; the scans byte for byte. None when it does not parse."""
    if raw[:2] != b"\xff\xd8":
        return None
    out, i, n = bytearray(raw[:2]), 2, len(raw)
    while i + 4 <= n:
        if raw[i] != 0xFF:
            return None
        marker = raw[i + 1]
        if marker == 0xFF:  # a fill byte before a marker
            i += 1
            continue
        if marker == 0xDA:  # the first scan: from here to the last end-of-image is the picture
            end = raw.rfind(b"\xff\xd9")
            return bytes(out + raw[i:end + 2]) if end > i else None
        length = int.from_bytes(raw[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > n:
            return None
        metadata = (0xE0 <= marker <= 0xEF and marker not in _JPEG_KEEP_APP) or marker == 0xFE
        if not metadata:
            out += raw[i:i + 2 + length]
        i += 2 + length
    return None


def stripped_png(raw: bytes) -> Optional[bytes]:
    """The PNG with only the chunks that make its pixels. None when it does not parse."""
    if raw[:8] != _PNG_SIGNATURE:
        return None
    out, i, n = bytearray(raw[:8]), 8, len(raw)
    while i + 12 <= n:
        length = int.from_bytes(raw[i:i + 4], "big")
        kind = raw[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            return None
        if kind in _PNG_KEEP:
            out += raw[i:end]
        if kind == b"IEND":
            return bytes(out)
        i = end
    return None


#: The modes a file's own bytes are kept in: what the renderers read as the
#: pixels they are (a CMYK JPEG's ink is converted; 16-bit grey is scaled).
_AS_IS = {"JPEG": frozenset({"RGB", "L"}), "PNG": frozenset({"RGB", "RGBA", "L", "LA", "P", "1"})}


def _same_pixels(data: bytes, image: Image.Image) -> bool:
    try:
        other = Image.open(io.BytesIO(data))
        other.load()
    except Exception:  # noqa: BLE001
        return False
    if other.size != image.size:
        return False
    rgba = "RGBA" if _has_alpha(image) or _has_alpha(other) else "RGB"
    return other.convert(rgba).tobytes() == image.convert(rgba).tobytes()


def _as_it_came(raw: bytes, fmt: str, decoded: Image.Image) -> Optional[PrintArt]:
    """The upload's own bytes without their metadata, when they are the same picture."""
    if decoded.mode not in _AS_IS.get(fmt, ()):
        return None
    stripped = stripped_jpeg(raw) if fmt == "JPEG" else stripped_png(raw)
    if stripped is None or not _same_pixels(stripped, decoded):
        return None
    return PrintArt(stripped, fmt, decoded.width, decoded.height)


def normalise(raw: bytes, *, max_bytes: int = MAX_IMAGE_BYTES, longest: Optional[int] = None) -> PrintArt:
    """`raw` made ready for print (the module's docstring), or the 413/422 that says why not."""
    image = opened(raw, max_bytes=max_bytes)
    fmt = (image.format or "").upper()
    size = target_size(image.size, longest)
    if fmt == "JPEG" and size != image.size:
        # Decoded straight at (about) the size it is going to be: several
        # times faster than a phone photo in full. Never below the target.
        try:
            image.draft(image.mode if image.mode in ("RGB", "L", "CMYK") else "RGB", (max(size), max(size)))
        except Exception:  # noqa: BLE001
            pass
    try:
        image.load()
        decoded = image
        try:
            rotate = (image.getexif().get(_ORIENTATION, 1) or 1) != 1
        except Exception:  # noqa: BLE001
            rotate = False
        if rotate:
            image = ImageOps.exif_transpose(image)
        in_srgb = _srgb(image)
        recoloured = in_srgb is not image
        image = _pixels(in_srgb)
    except CustomHTTPException:
        raise
    except Exception:
        raise _not_readable()
    size = target_size(image.size, longest)
    if size != image.size:
        image = image.resize(size, Image.Resampling.LANCZOS)  # premultiplied, so no dark fringes on alpha
    # Upright, at its size and in sRGB: the upload's own pixels.
    untouched = not rotate and not recoloured and image.size == decoded.size
    image = _fresh(image.copy() if image is decoded else image)

    if image.mode == "RGBA":
        candidates = [PrintArt(_png(image), "PNG", image.width, image.height)]
    else:
        candidates = [PrintArt(_jpeg(image, sharp=fmt != "JPEG"), "JPEG", image.width, image.height)]
    palette = exact_palette(image)
    if palette is not None:
        candidates.append(PrintArt(_png(_fresh(palette)), "PNG", image.width, image.height))
    if untouched:
        own = _as_it_came(raw, fmt, decoded)
        if own is not None:
            candidates.append(own)
    # The smallest; on a tie the later one, which is lossless.
    return min(reversed(candidates), key=lambda c: len(c.data))
