import asyncio
from datetime import datetime, timedelta, timezone
import traceback
import uuid
import multiprocessing
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


async def notify_error(request: Request, exc: Exception, track_id: str):
    """
    Enqueue error for async notification with distributed handling
    """
    if not settings.DISCORD_ERROR_WEBHOOK:
        return
    error_details = {
        "track_id": track_id,
        "path": request.url.path,
        "method": request.method,
        "traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
    }

    await send_discord_notification(error_details)


async def send_discord_notification(
    error_details: dict, max_retries: int = 2, retry_delay: float = 1.0
):
    """
    Send Discord notification with enhanced concurrency handling
    """
    if not settings.DISCORD_ERROR_WEBHOOK:
        return
    async with httpx.AsyncClient() as client:
        for attempt in range(max_retries):
            try:
                payload = {
                    "content": f"🚨 Internal Server Error Detected",
                    "embeds": [
                        {
                            "title": "Error Details",
                            "color": 16711680,
                            "fields": [
                                {
                                    "name": "Track ID",
                                    "value": error_details.get("track_id", "N/A"),
                                },
                                {
                                    "name": "Path",
                                    "value": error_details.get("path", "N/A"),
                                },
                                {
                                    "name": "Method",
                                    "value": error_details.get("method", "N/A"),
                                },
                            ],
                        }
                    ],
                }

                timestamp = (
                    datetime.now(timezone.utc)
                    .astimezone(timezone(timedelta(hours=5, minutes=30)))
                    .isoformat()
                )

                error_content = f"""
============================ ERROR DETAILS ============================

Timestamp: {timestamp}
Track ID: {error_details.get("track_id", "N/A")}
Path: {error_details.get("path", "N/A")}
Method: {error_details.get("method", "N/A")}

============================== TRACEBACK ==============================

{error_details.get("traceback", "")}

=======================================================================
"""

                files = {
                    # "payload_json": (None, str(payload), "application/json"),
                    "file": (
                        "traceback.txt",
                        error_content.encode("utf-8"),
                        "text/plain",
                    ),
                }

                response = await client.post(
                    settings.DISCORD_ERROR_WEBHOOK,
                    files=files,
                )
                response.raise_for_status()
                return
            except Exception as e:
                print(f"Discord notification attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (2**attempt))

        print("Failed to send Discord error notification after all retries")
