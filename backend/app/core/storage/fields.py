import asyncio
import boto3
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.types import TypeDecorator, String
from sqlalchemy.util.concurrency import await_only, in_greenlet
from PIL import Image, ImageOps
import io
import uuid
import os
from typing import Dict, Optional, Union


from app.config import settings
from app.core.storage import media
from app.response import CustomHTTPException

logger = logging.getLogger(__name__)


# One client for signing read URLs, made when this module loads.
#
# Signing is local work (no request leaves the process), but making a boto3
# client loads and validates its service model: about 4 ms, and it used to be
# done for every image column of every row read, so a page that read a few
# thousand events spent seconds here. Clients are safe to share across
# threads. Made at import rather than on first use, so a test that swaps
# `boto3.client` for a fake while it runs cannot leave the fake behind here.
_SIGNER = boto3.client(
    "s3",
    aws_access_key_id=settings.S3_ACCESS_KEY,
    aws_secret_access_key=settings.S3_SECRET_KEY,
)


# Signed read URLs, kept and handed out again for half their life.
#
# Even with one client, signing is about 70 µs a URL (botocore resolves the
# endpoint rules every time), and every image column of every row read signs
# four or five: a page of 24 event cards signed ~200 URLs. A URL is reused
# for the first half of its life, so whoever receives it has at least the
# other half to load the image, and the same image has the same address
# across page views, which lets browsers and CDNs cache it.
#
# Two lives. A private file (a ticket's or certificate's art, a signature, a
# profile picture, an event's files) is signed for an hour and reused for
# half an hour. A public picture -- what the public site, the app and emails
# show of an event or a club, columns declared `public=True` -- is signed
# for settings.PUBLIC_IMAGE_URL_TTL (a week, SigV4's ceiling) and reused for
# half of that: a link in an email or a cached page keeps working for days,
# and a page shows the same URL for days rather than a new signature on
# every request. Kept per (key, life): a key signed both ways never mixes.
_SIGN_SECONDS = 3600
_SIGNED_MAX = 40000  # ~0.8 KB each: ~32 MB at the cap
_signed: "OrderedDict[tuple[str, int], tuple[float, str]]" = OrderedDict()
_signed_lock = threading.Lock()


def signed_url(key: str, *, public: bool = False) -> str:
    """A presigned GET URL for `key`, valid for at least half its life
    again: 30 minutes for a private file, half of PUBLIC_IMAGE_URL_TTL
    (three and a half days) for a public picture."""
    ttl = settings.PUBLIC_IMAGE_URL_TTL if public else _SIGN_SECONDS
    slot = (key, ttl)
    now = time.monotonic()
    with _signed_lock:
        hit = _signed.get(slot)
        if hit is not None and hit[0] > now:
            _signed.move_to_end(slot)
            return hit[1]
    url = _SIGNER.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=ttl,
    )
    with _signed_lock:
        _signed[slot] = (now + ttl / 2, url)
        _signed.move_to_end(slot)
        while len(_signed) > _SIGNED_MAX:
            _signed.popitem(last=False)
    return url


# ─── Uploads ─────────────────────────────────────────────────────────────
#
# A picture is decoded, checked and sized before any of it is kept:
#
#   - its bytes are counted before anything decodes them;
#   - its pixels are counted from the header before the decoder runs, and a
#     picture over MAX_IMAGE_PIXELS is refused (a 20 KB PNG can claim to be
#     50,000 x 50,000 and take gigabytes to open);
#   - a JPEG is decoded straight at the size the variants need (`draft`),
#     which is several times faster than decoding a phone photo in full;
#   - the camera's orientation is applied (`exif_transpose`), so a portrait
#     taken on a phone is upright in every variant and in the PDFs, whose
#     renderer ignores EXIF;
#   - the original is kept byte for byte -- it used to be re-encoded at
#     JPEG's default quality 75, losing detail on every upload -- unless its
#     orientation has to be baked in or it is larger than ORIGINAL_MAX_SIDE.
#
# All of it runs off the event loop. It used to run inside the column's
# `process_bind_param`, during the flush, on the loop: a decode, a LANCZOS
# resize per variant and four S3 PUTs held every other request in the
# process. `upload` is the explicit step for code that saves an image; the
# column still accepts the old dict/bytes values, and processes them on a
# worker thread while the flush waits (see S3ImageField.process_bind_param).

MAX_IMAGE_PIXELS = 40_000_000
# Pillow's own guard: a warning above this, an error at twice it. Uploads are
# refused above it by `_check_pixels` before Pillow gets that far.
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
ORIGINAL_MAX_SIDE = 4096
_FORMATS = {"JPEG": "jpeg", "MPO": "jpeg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}
_CONTENT_TYPES = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}
_ORIENTATION = 0x0112


def _raw_of(value) -> tuple[bytes, Optional[str]]:
    """The bytes and file name of whatever a caller assigned to the column."""
    if isinstance(value, dict):
        if "file" in value:
            data, filename = value["file"].read(), getattr(value["file"], "filename", None)
        elif "bytes" in value:
            data, filename = value["bytes"], value.get("filename")
        else:
            raise ValueError("Invalid input format")
    elif isinstance(value, (bytes, bytearray, io.BytesIO)):
        data, filename = value, None
    else:
        raise ValueError("Unsupported input type")
    if isinstance(data, io.BytesIO):
        data.seek(0)
    if hasattr(data, "read"):
        data = data.read()
    return bytes(data), filename


def _too_big(limit: int) -> CustomHTTPException:
    return CustomHTTPException(413, message=f"That image is over {limit // (1024 * 1024)} MB. Export it smaller and try again.")


def _check_pixels(img: Image.Image) -> None:
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise CustomHTTPException(
            422,
            message=f"That image is {img.width} x {img.height} pixels. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.",
        )


def _fit(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """Within `box`, keeping the shape, never larger than the picture is."""
    w, h = size
    scale = min(box[0] / w, box[1] / h, 1.0)
    return max(1, round(w * scale)), max(1, round(h * scale))


def _encode(img: Image.Image, ext: str) -> bytes:
    out = io.BytesIO()
    if ext == "jpeg":
        if img.mode not in ("RGB", "L", "CMYK"):
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=88, optimize=True, progressive=True)
    elif ext == "png":
        img.save(out, format="PNG", optimize=True)
    elif ext == "webp":
        img.save(out, format="WEBP", quality=88, method=4)
    else:
        img.save(out, format=ext.upper())
    return out.getvalue()


@dataclass
class Prepared:
    """An upload, checked and sized, ready to store: name -> bytes."""

    ext: str
    original: bytes
    variants: Dict[str, bytes] = field(default_factory=dict)

    @property
    def content_type(self) -> str:
        return _CONTENT_TYPES.get(self.ext, f"image/{self.ext}")


def prepare_image(raw: bytes, *, max_size: Optional[int], allowed: Optional[list], variations: dict) -> Prepared:
    """Check one upload and make its variants. CPU only; no network."""
    if max_size and len(raw) > max_size:
        raise _too_big(max_size)
    try:
        img = Image.open(io.BytesIO(raw))
    except Image.DecompressionBombError:
        raise CustomHTTPException(422, message=f"That image is too large. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    except Exception:
        raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
    ext = _FORMATS.get((img.format or "").upper())
    if ext is None or (allowed and ext not in allowed and not (ext == "jpeg" and "jpg" in allowed)):
        raise CustomHTTPException(
            status_code=422,
            message=f"Unsupported image format. Allowed formats: {allowed}",
        )
    _check_pixels(img)

    try:
        orientation = img.getexif().get(_ORIENTATION, 1) or 1
    except Exception:
        orientation = 1
    rotate = orientation != 1
    oversized = max(img.size) > ORIGINAL_MAX_SIDE
    boxes = [(int(v["width"]), int(v["height"])) for v in variations.values() if v.get("width") and v.get("height")]
    need_full = rotate or oversized
    # Decode a JPEG only as large as anything made from it needs.
    if ext == "jpeg":
        side = ORIGINAL_MAX_SIDE if need_full else max((max(b) for b in boxes), default=0)
        if side and not (rotate and not oversized):
            img.draft(img.mode if img.mode in ("RGB", "L", "CMYK") else "RGB", (side, side))
    if not need_full and not boxes:
        # Nothing to make: the bytes are checked, and kept as they are.
        try:
            # On a fresh handle: verify() must come straight after open().
            Image.open(io.BytesIO(raw)).verify()
        except Exception:
            raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
        return Prepared(ext=ext, original=raw)

    try:
        img.load()
        upright = ImageOps.exif_transpose(img) if rotate else img
    except Image.DecompressionBombError:
        raise CustomHTTPException(422, message=f"That image is too large. Use one under {MAX_IMAGE_PIXELS // 1_000_000} megapixels.")
    except Exception:
        raise CustomHTTPException(422, message="That file is not an image we can read. Use PNG, JPEG or WebP.")
    if upright.mode == "P" and ext != "gif":
        upright = upright.convert("RGBA")

    original = raw
    if need_full:
        whole = upright
        if max(whole.size) > ORIGINAL_MAX_SIDE:
            whole = whole.resize(_fit(whole.size, (ORIGINAL_MAX_SIDE, ORIGINAL_MAX_SIDE)), Image.Resampling.LANCZOS)
        if ext == "gif":
            # An animation is kept whole; a sideways GIF is rare enough to leave.
            original = raw
        else:
            original = _encode(whole, ext)

    variants = {}
    for name, spec in variations.items():
        width, height = spec.get("width"), spec.get("height")
        if not (width and height):
            continue
        size = _fit(upright.size, (int(width), int(height)))
        resized = upright if size == upright.size else upright.resize(size, Image.Resampling.LANCZOS)
        variants[name] = _encode(resized, ext)
    return Prepared(ext=ext, original=original, variants=variants)


def variant_key(key: str, name: str) -> str:
    """Where the column keeps variant `name` of the original at `key`."""
    folder, _, filename = key.rpartition("/")
    stem, _, ext = filename.rpartition(".")
    return f"{folder}/{stem}.{name}.{ext}" if folder else f"{stem}.{name}.{ext}"


def stored_key(value) -> Optional[str]:
    """The key behind whatever an image or file column holds: what was read
    (S3Image, S3File), or a key just assigned."""
    if value is None:
        return None
    variations = getattr(value, "variations", None)
    if isinstance(variations, dict):
        return variations.get("original")
    path = getattr(value, "file_path", None)
    if path:
        return path
    if isinstance(value, str):
        return value or None
    return None


class S3Image(dict):
    """A picture read from an image column: its links by size, as a dict
    (what a response serialises); `variations` holds the keys behind them,
    and `widths` the box each size was made to fit, for the sizes that exist
    (media.srcset)."""

    def __init__(self, variations, client=None, widths=None, **kwargs):
        self.variations = variations
        self.widths = widths or {}
        self._client = client
        super().__init__(kwargs)

    def _delete_now(self, keys) -> None:
        s3_client = self._client or _SIGNER
        for value in keys:
            try:
                s3_client.delete_object(Bucket=settings.S3_BUCKET, Key=value)
            except Exception as e:
                logger.warning(f"Error deleting S3 object: {str(e)}")

    def delete(self):
        """Delete the original and its variants from the bucket. Inside the
        event loop the deletes go to a worker thread rather than hold it."""
        keys = list(self.variations.values())
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            self._delete_now(keys)
        else:
            loop.run_in_executor(None, self._delete_now, keys)


class S3File(str):
    def __new__(cls, file_path, client=None):
        url = signed_url(file_path)
        obj = super().__new__(cls, url)
        obj.file_path = file_path
        obj._client = client
        return obj

    def delete(self):
        s3_client = getattr(self, "_client", None) or _SIGNER
        try:
            s3_client.delete_object(Bucket=settings.S3_BUCKET, Key=self.file_path)
        except Exception as e:
            logger.warning(f"Error deleting S3 object: {str(e)}")


def _field_of(column):
    """The S3ImageField/S3FileField behind a mapped attribute or a Column."""
    if isinstance(column, (S3ImageField, S3FileField)):
        return column
    prop = getattr(column, "property", None)
    if prop is not None and getattr(prop, "columns", None):
        return prop.columns[0].type
    return column.type


async def upload(column, value) -> Optional[str]:
    """The explicit upload step: check, size and store `value` for `column`
    off the event loop, and answer with the key to assign to it.

        template.logo = await upload(TicketTemplates.logo, {"bytes": data, "filename": name})

    The decoding and resizing run on the render pool, the PUTs on a worker
    thread. A key (or nothing) is returned as it is.
    """
    target = _field_of(column)
    if not value:
        return None
    if isinstance(value, str):
        return value
    passed = stored_key(value)
    if passed:
        return passed
    if isinstance(target, S3ImageField):
        from app.core.layout import pool

        raw, filename = _raw_of(value)
        prepared = await pool.run(target.prepare, raw)
        return await asyncio.to_thread(target.store, prepared)
    return await asyncio.to_thread(target.upload, value)


class S3ImageField(TypeDecorator):
    """
    A custom SQLAlchemy field for handling images with S3 storage.
    Stores the original's key; its variants sit beside it as
    `{uuid}.{variant}.{ext}`.

    `public=True` for a picture anyone may see (an event's poster, a club's
    logo): its links are signed for PUBLIC_IMAGE_URL_TTL rather than an hour
    (see signed_url), or are on the media CDN when it is on (media.public_url).

    Every upload is stored under a versioned name (`v-<uuid4 hex>.<ext>`,
    media.versioned_name): a key is written once and never again, so the
    CDN may keep it for a year.

    `print_art=True` for a picture drawn into documents (a ticket's or
    certificate's logo, background or signature): the upload is stored
    ready for print instead of as sent (app/core/storage/doc_art.py), a JPEG
    or a PNG, upright, sized and without metadata. Such a column has no
    variants.
    """

    impl = String
    cache_ok = True

    def __init__(
        self,
        upload_to: str = "uploads",
        max_size: int = 10 * 1024 * 1024,
        allowed_extensions: list[str] = ["jpg", "jpeg", "png", "gif", "webp"],
        variations: dict = {},
        public: bool = False,
        print_art: bool = False,
    ):
        super().__init__()
        self.public = public
        self.print_art = print_art
        self.max_size = max_size
        self.allowed_extensions = allowed_extensions
        self.bucket_name = settings.S3_BUCKET
        self.base_path = os.path.join(settings.S3_BASE_PATH, upload_to)
        # The column's own folder, without the base path: a key keeps its
        # folder when the base path setting changes.
        self.folder = upload_to.strip("/")
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )
        self.variations = variations

    # The two halves of an upload: `prepare` is CPU (no network), `store` is
    # the PUTs. `upload` is both, for a caller already off the loop.

    def prepare(self, raw: bytes) -> Prepared:
        if getattr(self, "print_art", False):
            from app.core.storage import doc_art

            art = doc_art.normalise(raw, max_bytes=self.max_size)
            return Prepared(ext=art.ext(jpeg="jpeg"), original=art.data)
        return prepare_image(raw, max_size=self.max_size, allowed=self.allowed_extensions, variations=self.variations)

    def store(self, prepared: Prepared) -> str:
        full_path = os.path.join(self.base_path, media.versioned_name(prepared.ext))
        for name, data in [(None, prepared.original), *prepared.variants.items()]:
            self.s3_client.upload_fileobj(
                io.BytesIO(data),
                self.bucket_name,
                full_path if name is None else variant_key(full_path, name),
                ExtraArgs={"ContentType": prepared.content_type},
            )
        return full_path

    def upload(self, value) -> str:
        raw, _ = _raw_of(value)
        return self.store(self.prepare(raw))

    def process_bind_param(
        self, value: Union[Dict, bytes, io.BytesIO], dialect
    ) -> Optional[str]:
        """Process the value before saving to database.

        A key is stored as it is, and so is an image read from another row
        (copying a template's logo onto its fork used to fail here: an
        S3Image is a dict without "bytes"). Anything else is an upload. Inside
        an async session's flush this runs on the event loop, so the upload
        is handed to `upload` and the flush waits for it without holding the
        loop; outside one (a script, a sync session) it runs here.
        """

        if not value:
            return None

        if isinstance(value, str):
            return value

        passed = stored_key(value)
        if passed:
            return passed

        try:
            key = await_only(upload(self, value)) if in_greenlet() else self.upload(value)
        except CustomHTTPException as e:
            raise e
        except Exception as e:
            raise ValueError(f"Error processing image: {str(e)}")
        _remember_upload(value, key)
        return key

    def has_variants(self, key: str) -> bool:
        """Whether the variants of `key` were made, as far as the key says.

        Every upload through the column makes them all, beside the original
        in the column's folder. A key from anywhere else -- another column's
        folder, a designer upload, an address -- has none, and a link to a
        variant that is not there is a broken image. Decided from the key
        alone (a HEAD per variant per row read is not affordable): one in
        the column's own folder, under whatever base path it was stored.
        """
        if not self.variations or "://" in key.replace(settings.S3_BASE_PATH, "", 1):
            return False
        folder = key.rpartition("/")[0].strip("/")
        return bool(self.folder) and (folder == self.folder or folder.endswith("/" + self.folder))

    def process_result_value(self, value: str, dialect) -> Optional[str]:
        """Process the value when retrieving from database.

        A variant that exists gets its own link; one that does not (see
        `has_variants`) gets the original's, so a client reading
        `poster["medium"]` still shows the picture rather than a 404.
        """
        if not value:
            return None

        # A public picture's links come from the one builder: on the CDN when
        # it is on, else signed for a week. A private file's, for an hour.
        link = media.public_url if getattr(self, "public", False) else signed_url
        original = link(value)
        variations = {}
        variation_paths = {}
        widths = {}
        own = self.has_variants(value)
        for variation, spec in self.variations.items():
            if own:
                path = variant_key(value, variation)
                variation_paths[variation] = path
                variations[variation] = link(path)
                if spec.get("width"):
                    widths[variation] = int(spec["width"])
            else:
                variations[variation] = original

        variations["original"] = original
        variation_paths["original"] = value
        return S3Image(variations=variation_paths, client=self.s3_client, widths=widths, **variations)


class S3FileField(TypeDecorator):
    """
    A custom SQLAlchemy field for handling file uploads with S3 storage.
    Stores the file path and handles direct file uploads without variants.
    """

    impl = String
    cache_ok = True

    def __init__(
        self,
        upload_to: str = "uploads",
        max_size: int = 50 * 1024 * 1024,  # 50MB default
        allowed_extensions: Optional[list[str]] = None,
    ):
        super().__init__()
        self.max_size = max_size
        self.allowed_extensions = allowed_extensions
        self.bucket_name = settings.S3_BUCKET
        self.base_path = os.path.join(settings.S3_BASE_PATH, upload_to)
        self.s3_client = boto3.client(
            "s3",
            aws_access_key_id=settings.S3_ACCESS_KEY,
            aws_secret_access_key=settings.S3_SECRET_KEY,
        )

    def upload(self, value) -> str:
        """Check and store one file; the key. Blocking: see `upload()`."""
        if isinstance(value, dict):
            if "file" in value:
                file_data = value["file"].read()
                filename = value["file"].filename
            elif "bytes" in value:
                file_data = value["bytes"]
                filename = value.get("filename")
            else:
                raise ValueError("Invalid input format")
        elif isinstance(value, (bytes, io.BytesIO)):
            file_data = value
            filename = None
        else:
            raise ValueError("Unsupported input type")

        if not filename:
            raise ValueError("Filename is required")

        # Check file extension
        ext = filename.split(".")[-1].lower()
        if self.allowed_extensions and ext not in self.allowed_extensions:
            raise ValueError(
                f"Unsupported file extension. Allowed extensions: {self.allowed_extensions}"
            )

        # Generate unique filename
        full_path = os.path.join(self.base_path, f"{uuid.uuid4()}.{ext}")

        # Check file size
        if isinstance(file_data, bytes):
            file_size = len(file_data)
            buffer = io.BytesIO(file_data)
        else:
            file_data.seek(0, os.SEEK_END)
            file_size = file_data.tell()
            file_data.seek(0)
            buffer = file_data

        if self.max_size and file_size > self.max_size:
            raise ValueError(
                f"File size exceeds maximum allowed size of {self.max_size} bytes"
            )

        self.s3_client.upload_fileobj(buffer, self.bucket_name, full_path)
        return full_path

    def process_bind_param(
        self, value: Union[Dict, bytes, io.BytesIO], dialect
    ) -> Optional[str]:
        """Process the value before saving to database: a key as it is, an
        upload off the event loop (see S3ImageField.process_bind_param)."""
        if not value:
            return None

        if isinstance(value, str):
            return value

        passed = stored_key(value)
        if passed:
            return passed

        try:
            key = await_only(upload(self, value)) if in_greenlet() else self.upload(value)
        except Exception as e:
            raise ValueError(f"Error processing file: {str(e)}")
        _remember_upload(value, key)
        return key

    def process_result_value(self, value: str, dialect) -> Optional[S3File]:
        """Process the value when retrieving from database."""
        if not value:
            return None
        return S3File(value, client=self.s3_client)


# ─── After a flush, an uploaded column reads as it would from the database ───
#
# A service that assigns an upload ({"bytes": ..., "filename": ...}) and
# commits keeps that dict on the object -- the sessions do not expire on
# commit -- so a response built from the object afterwards serialised the
# raw upload. Once the flush has stored the key, the attribute is set to what
# reading the row back gives: the S3Image (or S3File), with its links.

_UPLOADED_MAX = 1000
_uploaded: "OrderedDict[int, tuple[object, str]]" = OrderedDict()
_uploaded_lock = threading.Lock()
_S3_ATTRS: Dict[object, tuple] = {}


def _remember_upload(value, key: str) -> None:
    with _uploaded_lock:
        # The value is held too, so its id cannot be reused while it is here.
        _uploaded[id(value)] = (value, key)
        while len(_uploaded) > _UPLOADED_MAX:
            _uploaded.popitem(last=False)


def _s3_attrs(mapper) -> tuple:
    """The (attribute, column type) pairs of a mapper's image and file columns."""
    found = _S3_ATTRS.get(mapper)
    if found is None:
        found = tuple(
            (prop.key, prop.columns[0].type)
            for prop in mapper.column_attrs
            if isinstance(prop.columns[0].type, (S3ImageField, S3FileField))
        )
        _S3_ATTRS[mapper] = found
    return found


def _is_upload(value) -> bool:
    return bool(value) and not isinstance(value, str) and stored_key(value) is None


@event.listens_for(Session, "before_flush")
def _note_uploads(session, flush_context, instances) -> None:
    pending = []
    for obj in (*session.new, *session.dirty):
        mapper = getattr(obj, "__mapper__", None)
        if mapper is None:
            continue
        for attr, field_type in _s3_attrs(mapper):
            value = obj.__dict__.get(attr)
            if _is_upload(value):
                pending.append((obj, attr, field_type, value))
    # Replaced, never added to: a flush that failed left nothing worth keeping.
    if pending:
        session.info["_s3_uploads"] = pending
    else:
        session.info.pop("_s3_uploads", None)


@event.listens_for(Session, "after_flush_postexec")
def _show_uploads(session, flush_context) -> None:
    pending = session.info.pop("_s3_uploads", None)
    if not pending:
        return
    for obj, attr, field_type, value in pending:
        with _uploaded_lock:
            hit = _uploaded.pop(id(value), None)
        if hit is None or hit[0] is not value or obj.__dict__.get(attr) is not value:
            continue
        set_committed_value(obj, attr, field_type.process_result_value(hit[1], None))
