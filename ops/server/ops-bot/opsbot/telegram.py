"""A small Telegram Bot API client (standard library only).

- Sends only ever go to the one configured chat.
- The token is in the request URL only; every error message that leaves this
  module has it scrubbed out.
- sendDocument streams the file (no 50 MB string in memory) and refuses
  anything that is not an age-encrypted file.
- Retries network errors and 5xx with backoff, honours 429 retry_after, and
  rate-limits what it sends so a bad hour can never turn into a flood.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import secrets as _secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .redact import scrub_secret

log = logging.getLogger("opsbot.telegram")

API_HOST = "api.telegram.org"
#: Telegram's limit for a bot's uploads is 50 MB; a little under, for the
#: multipart envelope.
UPLOAD_LIMIT = 50 * 1000 * 1000 - 64 * 1024
MESSAGE_LIMIT = 4096
CAPTION_LIMIT = 1024
AGE_HEADER = b"age-encryption.org/v1\n"


class TelegramError(Exception):
    def __init__(self, message: str, *, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class NotEncrypted(Exception):
    """sendDocument was handed something that is not an age file."""


def is_age_file(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(AGE_HEADER)) == AGE_HEADER
    except OSError:
        return False


class RateLimiter:
    """At least `min_interval` seconds between sends, and at most
    `per_window` sends per `window` seconds; over that, sends are dropped
    (and counted, so the next message says so)."""

    def __init__(self, min_interval: float = 1.1, per_window: int = 20, window: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self.per_window = per_window
        self.window = window
        self.clock = clock
        self.sleep = sleep
        self.sent: list[float] = []
        self.dropped = 0
        self.lock = threading.Lock()

    def acquire(self) -> bool:
        with self.lock:
            now = self.clock()
            self.sent = [t for t in self.sent if now - t < self.window]
            if len(self.sent) >= self.per_window:
                self.dropped += 1
                return False
            if self.sent and now - self.sent[-1] < self.min_interval:
                self.sleep(self.min_interval - (now - self.sent[-1]))
                now = self.clock()
            self.sent.append(now)
            return True

    def take_dropped(self) -> int:
        with self.lock:
            dropped, self.dropped = self.dropped, 0
            return dropped


class Telegram:
    def __init__(self, token: str, chat_id: int, *, host: str = API_HOST, limiter: RateLimiter | None = None, sleep=time.sleep):
        self._token = token
        self.chat_id = chat_id
        self.host = host
        self.limiter = limiter or RateLimiter()
        self.sleep = sleep
        self._ssl = ssl.create_default_context()

    # -- transport (replaced in tests) ------------------------------------

    def _url(self, method: str) -> str:
        return f"https://{self.host}/bot{self._token}/{method}"

    def _post_json(self, method: str, params: dict, timeout: float) -> dict:
        data = json.dumps(params).encode()
        request = urllib.request.Request(self._url(method), data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self._ssl) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            try:
                return json.loads(error.read().decode())
            except Exception:  # noqa: BLE001 -- not JSON: report the status
                raise TelegramError(f"HTTP {error.code}", status=error.code) from None

    def _post_multipart(self, method: str, fields: dict[str, str], file_field: str, path: Path, filename: str, timeout: float) -> dict:
        boundary = "moa" + _secrets.token_hex(16)
        head = b""
        for name, value in fields.items():
            head += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
        head += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = os.path.getsize(path)
        connection = http.client.HTTPSConnection(self.host, timeout=timeout, context=self._ssl)
        try:
            connection.putrequest("POST", f"/bot{self._token}/{method}")
            connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            connection.putheader("Content-Length", str(len(head) + size + len(tail)))
            connection.endheaders()
            connection.send(head)
            with open(path, "rb") as handle:
                while chunk := handle.read(256 * 1024):
                    connection.send(chunk)
            connection.send(tail)
            response = connection.getresponse()
            body = response.read().decode(errors="replace")
            try:
                return json.loads(body)
            except ValueError:
                raise TelegramError(f"HTTP {response.status}", status=response.status) from None
        finally:
            connection.close()

    # -- API ---------------------------------------------------------------

    def _clean(self, error: BaseException) -> str:
        return scrub_secret(f"{type(error).__name__}: {error}", self._token)[:300]

    def call(self, method: str, params: dict, *, timeout: float = 30, retries: int = 3) -> dict:
        """The API's `result`, or TelegramError. Network errors, 5xx and 429
        are retried `retries` times with backoff."""
        attempt = 0
        while True:
            attempt += 1
            try:
                answer = self._post_json(method, params, timeout)
            except TelegramError as error:
                failure = error
            except (OSError, http.client.HTTPException, ValueError) as error:
                failure = TelegramError(self._clean(error))
            else:
                if answer.get("ok"):
                    return answer.get("result")
                status = answer.get("error_code")
                retry_after = (answer.get("parameters") or {}).get("retry_after")
                description = scrub_secret(str(answer.get("description", "")), self._token)[:300]
                failure = TelegramError(f"{method}: {status} {description}", status=status, retry_after=retry_after)
            retryable = failure.status is None or failure.status == 429 or failure.status >= 500
            if not retryable or attempt > retries:
                raise failure
            delay = min(float(failure.retry_after or 0) or 2.0**attempt, 60.0)
            log.warning("telegram %s failed (%s); retrying in %.0fs", method, failure, delay)
            self.sleep(delay)

    def send_message(self, text: str, *, html: bool = True) -> bool:
        """Send text to the owner's chat. False (logged) when it could not be
        sent or the rate limit dropped it -- never raises."""
        if not self.limiter.acquire():
            log.warning("telegram: rate limit reached, a message was dropped")
            return False
        dropped = self.limiter.take_dropped()
        if dropped:
            text = f"({dropped} earlier message(s) dropped by the bot's rate limit)\n" + text
        if len(text) > MESSAGE_LIMIT:
            text = text[: MESSAGE_LIMIT - 20] + "\n[...cut...]"
            if html and "<pre>" in text and "</pre>" not in text[text.rfind("<pre>") :]:
                text += "</pre>"
        params = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if html:
            params["parse_mode"] = "HTML"
        try:
            self.call("sendMessage", params)
            return True
        except TelegramError as error:
            log.error("telegram: could not send a message: %s", error)
            return False

    def send_document(self, path: Path, caption: str, *, filename: str | None = None, timeout: float = 300) -> None:
        """Upload an age-encrypted file to the owner's chat. Raises
        NotEncrypted for anything else, TelegramError when it fails."""
        path = Path(path)
        if not is_age_file(path):
            raise NotEncrypted(f"{path.name} is not an age-encrypted file; refusing to send it")
        if path.stat().st_size > UPLOAD_LIMIT:
            raise TelegramError(f"{path.name} is over Telegram's 50 MB limit for bots", status=413)
        self.limiter.acquire()
        fields = {"chat_id": str(self.chat_id), "caption": caption[:CAPTION_LIMIT], "parse_mode": "HTML"}
        name = filename or path.name
        attempt = 0
        while True:
            attempt += 1
            try:
                answer = self._post_multipart("sendDocument", fields, "document", path, name, timeout)
            except TelegramError as error:
                failure = error
            except (OSError, http.client.HTTPException) as error:
                failure = TelegramError(self._clean(error))
            else:
                if answer.get("ok"):
                    return
                status = answer.get("error_code")
                description = scrub_secret(str(answer.get("description", "")), self._token)[:300]
                failure = TelegramError(
                    f"sendDocument: {status} {description}",
                    status=status,
                    retry_after=(answer.get("parameters") or {}).get("retry_after"),
                )
            retryable = failure.status is None or failure.status == 429 or failure.status >= 500
            if not retryable or attempt >= 3:
                raise failure
            delay = min(float(failure.retry_after or 0) or 5.0 * attempt, 60.0)
            log.warning("telegram sendDocument failed (%s); retrying in %.0fs", failure, delay)
            self.sleep(delay)

    def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict]:
        """Long poll. Raises TelegramError (no retries: the caller backs off)."""
        params: dict = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 15, retries=0) or []
