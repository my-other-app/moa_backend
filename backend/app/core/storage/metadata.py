"""A kept upload's own bytes, without the metadata that travels with them.

A phone photo carries where it was taken (EXIF's GPS block), the phone, the
time and often a thumbnail; an exported logo or poster carries its editor's
XMP, IPTC captions and comments. An upload kept byte for byte (an image
column's original, an email picture, a product photo: storage.fields,
storage.images, merch.service) kept all of it, on pictures anyone may see.

`without_metadata` takes it out losslessly: the picture's own data (a JPEG's
scans, a PNG's IDAT, a WebP's VP8/VP8L/ALPH/animation frames) is copied
byte for byte, so the stored file decodes to exactly the same pixels, and
the ICC colour profile is kept, so it shows in the same colours. Nothing is
decoded or encoded here.

- **JPEG:** APP0 (JFIF) and APP14 (Adobe, which says how the colour is to be
  read) are kept, and APP2 when it is a piece of the ICC profile. Every other
  APPn (APP1's EXIF and XMP, APP2's multi-picture index, APP13's Photoshop
  and IPTC...) and every comment goes. A multi-picture file (MPO: a camera's
  JPEG with a second frame, which has its own EXIF) is cut after its first
  picture.
- **PNG:** the chunks that make or colour the picture are kept (IHDR, PLTE,
  tRNS, IDAT, IEND; iCCP, sRGB, gAMA, cHRM, cICP, mDCV, cLLI, sBIT, bKGD,
  pHYs; an APNG's acTL, fcTL and fdAT); the rest (eXIf, tEXt, zTXt, iTXt with
  XMP, tIME, caBX...) go.
- **WebP:** the EXIF and XMP chunks go, and the extended header's flags for
  them are cleared; ICCP, ALPH and the animation chunks stay.

**Orientation.** EXIF also says which way up a photo was taken. Taking it
out without turning the picture first would leave a portrait on its side, so
a picture whose orientation is not 1 is refused here (ValueError): every
caller turns such a picture (ImageOps.exif_transpose) and encodes it anew,
and a new encoding carries no metadata (Pillow writes EXIF and XMP only when
asked to).
"""
from __future__ import annotations

import io
import struct

from PIL import Image

_ORIENTATION = 0x0112

#: What every encode passes Pillow so the new file carries none of the old
#: one's metadata: Pillow writes EXIF and XMP only when given them, but a
#: JPEG's or GIF's comment it copies from the picture it was read from.
NO_METADATA = {"exif": b"", "xmp": b"", "comment": b""}


def keep_colour(image: Image.Image) -> dict:
    """The colour profile to pass an encode, so the new file shows in the
    same colours (Pillow keeps a PNG's itself, not a JPEG's or a WebP's)."""
    icc = image.info.get("icc_profile")
    return {"icc_profile": icc} if icc else {}


def without_metadata(raw: bytes, fmt: str) -> bytes:
    """`raw`, a picture Pillow named `fmt` ("JPEG", "MPO", "PNG" or "WEBP"),
    with its metadata taken out and nothing else changed; other formats come
    back as they are. ValueError when its EXIF orientation is not 1 (turn it
    first) or its structure does not parse."""
    fmt = (fmt or "").upper()
    if fmt not in ("JPEG", "MPO", "PNG", "WEBP"):
        return raw
    try:
        orientation = Image.open(io.BytesIO(raw)).getexif().get(_ORIENTATION, 1) or 1
    except Exception:  # noqa: BLE001 - EXIF that cannot be read cannot turn the picture either
        orientation = 1
    if orientation != 1:
        raise ValueError("The picture must be turned upright before its metadata goes")
    if fmt in ("JPEG", "MPO"):
        return _jpeg(raw)
    if fmt == "PNG":
        return _png(raw)
    return _webp(raw)


# ─── JPEG ─────────────────────────────────────────────────────────────────

#: Markers with no length: SOI, EOI, TEM and the restart markers.
_STANDALONE = frozenset({0xD8, 0xD9, 0x01, *range(0xD0, 0xD8)})


def _keep_jpeg_segment(marker: int, body: bytes) -> bool:
    if marker == 0xFE:  # a comment
        return False
    if 0xE0 <= marker <= 0xEF:
        if marker == 0xE0:
            return body.startswith((b"JFIF\x00", b"JFXX\x00"))
        if marker == 0xE2:
            return body.startswith(b"ICC_PROFILE\x00")
        return marker == 0xEE  # Adobe: how the colour is to be read
    return True


def _jpeg(raw: bytes) -> bytes:
    if raw[:2] != b"\xff\xd8":
        raise ValueError("Not a JPEG")
    out = bytearray(raw[:2])
    i, n = 2, len(raw)
    scanned = False
    while i < n:
        if raw[i] != 0xFF:
            raise ValueError("A JPEG segment did not start with a marker")
        while i < n and raw[i] == 0xFF:  # fill bytes before a marker
            i += 1
        if i >= n:
            break
        marker = raw[i]
        i += 1
        if marker == 0xD9:  # the end of the first picture: anything after it (an MPO's second frame) goes
            if not scanned:
                raise ValueError("A JPEG with no picture in it")
            out += b"\xff\xd9"
            return bytes(out)
        if marker in _STANDALONE:
            out += bytes((0xFF, marker))
            continue
        if i + 2 > n:
            raise ValueError("A JPEG segment is cut short")
        length = int.from_bytes(raw[i:i + 2], "big")
        if length < 2 or i + length > n:
            raise ValueError("A JPEG segment runs past the end")
        body = raw[i + 2:i + length]
        if _keep_jpeg_segment(marker, body):
            out += bytes((0xFF, marker)) + raw[i:i + length]
        i += length
        if marker == 0xDA:  # a scan: its entropy-coded data runs to the next marker
            scanned = True
            start = i
            while i < n:
                if raw[i] == 0xFF and i + 1 < n and raw[i + 1] != 0x00 and not 0xD0 <= raw[i + 1] <= 0xD7 and raw[i + 1] != 0xFF:
                    break
                i += 1
            out += raw[start:i]
    raise ValueError("A JPEG with no end")


# ─── PNG ──────────────────────────────────────────────────────────────────

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_KEEP = frozenset({
    b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND",
    b"iCCP", b"sRGB", b"gAMA", b"cHRM", b"cICP", b"mDCV", b"cLLI", b"sBIT", b"bKGD", b"pHYs",
    b"acTL", b"fcTL", b"fdAT",
})


def _png(raw: bytes) -> bytes:
    if raw[:8] != _PNG_SIGNATURE:
        raise ValueError("Not a PNG")
    out = bytearray(raw[:8])
    i, n = 8, len(raw)
    while i + 12 <= n:
        length = int.from_bytes(raw[i:i + 4], "big")
        kind = raw[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            raise ValueError("A PNG chunk runs past the end")
        if kind in _PNG_KEEP:
            out += raw[i:end]
        if kind == b"IEND":
            return bytes(out)
        i = end
    raise ValueError("A PNG with no end")


# ─── WebP ─────────────────────────────────────────────────────────────────

_WEBP_KEEP = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANIM", b"ANMF", b"ICCP"})
_VP8X_EXIF, _VP8X_XMP = 0x08, 0x04


def _webp(raw: bytes) -> bytes:
    if raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        raise ValueError("Not a WebP")
    end = min(len(raw), 8 + int.from_bytes(raw[4:8], "little"))
    chunks = bytearray()
    kinds = set()
    i = 12
    while i + 8 <= end:
        kind = raw[i:i + 4]
        size = int.from_bytes(raw[i + 4:i + 8], "little")
        stop = i + 8 + size + (size & 1)
        if i + 8 + size > end:
            raise ValueError("A WebP chunk runs past the end")
        if kind in _WEBP_KEEP:
            kinds.add(kind)
            chunk = bytearray(raw[i:min(stop, end)])
            if len(chunk) < 8 + size + (size & 1):
                chunk += b"\x00"  # the pad byte a chunk of odd size ends with
            if kind == b"VP8X" and size >= 1:
                chunk[8] &= ~(_VP8X_EXIF | _VP8X_XMP) & 0xFF
            chunks += chunk
        i = stop
    if not kinds & {b"VP8 ", b"VP8L", b"ANMF"}:
        raise ValueError("A WebP with no picture in it")
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WEBP" + bytes(chunks)
