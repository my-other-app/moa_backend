"""A guest list or an organisation list from an uploaded CSV or Excel file.

The upload is read up to IMPORT_MAX_MB (a bigger file is 413). A workbook
(.xlsx) is a zip: its parts are checked before anything is unpacked, so a
small file that would unpack to far more than a sheet ever holds is refused
too (the sizes a zip declares are the most it unpacks: zipfile stops there).
The sheet is parsed on a worker thread, so a large one never holds up the
API's other requests, and at most IMPORT_MAX_ROWS rows are taken (more is
413 too, before the rest is read).
"""

from __future__ import annotations

import asyncio
import zipfile
from io import BytesIO

import pandas as pd
from fastapi import UploadFile

from app.config import settings
from app.response import CustomHTTPException

#: The most parts a workbook may have (its sheets, styles, strings...).
MAX_ZIP_MEMBERS = 1000
#: Bytes read from the upload at a time.
_CHUNK = 1024 * 1024
_MB = 1024 * 1024


def _too_large(message: str) -> CustomHTTPException:
    return CustomHTTPException(413, message=message, error_code="FILE_TOO_LARGE")


async def read_upload(file: UploadFile, limit: int | None = None) -> bytes:
    """The upload's bytes; 413 when there are more than `limit` (IMPORT_MAX_MB)."""
    limit = limit or max(1, int(settings.IMPORT_MAX_MB)) * _MB
    parts, size = [], 0
    while True:
        part = await file.read(_CHUNK)
        if not part:
            break
        size += len(part)
        if size > limit:
            raise _too_large(
                f"The file is larger than {max(1, limit // _MB)} MB. Split it and import each part."
            )
        parts.append(part)
    return b"".join(parts)


def check_workbook(contents: bytes) -> None:
    """413 for a workbook that would unpack to more than IMPORT_MAX_UNZIPPED_MB,
    or has more parts than a workbook needs. Something that is not a zip is
    left to the parser to refuse."""
    try:
        with zipfile.ZipFile(BytesIO(contents)) as book:
            members = book.infolist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return
    unpacked = sum(max(0, m.file_size) for m in members)
    if (
        len(members) > MAX_ZIP_MEMBERS
        or unpacked > max(1, int(settings.IMPORT_MAX_UNZIPPED_MB)) * _MB
    ):
        raise _too_large(
            "This workbook unpacks to more than a sheet of guests needs. Save it as CSV and try again."
        )


def parse(contents: bytes, filename: str) -> pd.DataFrame:
    """The sheet as a DataFrame (blocking: run it on a thread). At most
    IMPORT_MAX_ROWS rows are read; a sheet with more is 413."""
    limit = max(1, int(settings.IMPORT_MAX_ROWS))
    if filename.endswith(".csv"):
        df = pd.read_csv(BytesIO(contents), nrows=limit + 1)
    elif filename.endswith((".xls", ".xlsx")):
        if filename.endswith(".xlsx"):
            check_workbook(contents)
        df = pd.read_excel(BytesIO(contents), nrows=limit + 1)
    else:
        raise CustomHTTPException(400, "Unsupportedd file format")
    if len(df) > limit:
        raise CustomHTTPException(
            413,
            message=f"At most {limit:,} rows at a time. Split the file and import each part.",
            error_code="TOO_MANY_ROWS",
        )
    return df


async def read_excel(file: UploadFile) -> pd.DataFrame:
    """Reads an uploaded CSV or Excel file and returns a DataFrame."""
    filename = file.filename or ""
    if not filename.endswith((".csv", ".xls", ".xlsx")):
        raise CustomHTTPException(400, "Unsupportedd file format")
    contents = await read_upload(file)
    return await asyncio.to_thread(parse, contents, filename)
