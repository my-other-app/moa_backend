"""Mail through SES's SMTP interface: MAIL_PROVIDER=ses_smtp.

For a server that holds SES SMTP credentials (the SMTP username and password
SES made for an IAM user) and no API key. An SMTP password cannot be turned
back into a secret key, so this speaks SMTP to SES itself.

What is sent is what the API path sends (`transport.ses_envelope`): the same
raw MIME, the same sender and every To, Cc and Bcc as recipients. What comes
back is the same too: SES's message id, which the SES hook (/hooks/ses)
later matches its delivery, bounce and complaint notices by. SES gives that
id in its reply to the end of DATA, `250 Ok <id>`; smtplib's sendmail() drops
the reply, so the transaction is spoken command by command (mail, rcpt, data).

Connections:

- STARTTLS, always, with the certificate and the host name checked
  (`ssl.create_default_context`), before AUTH. A server that does not offer
  STARTTLS is refused and the password is never sent.
- One authenticated connection per thread, reused by the next mail on that
  thread. Sends run off the event loop -- on the mail threads
  (`pace.blocking`) or `asyncio.to_thread`, as the boto3 call does -- and a
  connection is never handed to another thread.
- SES closes a connection idle for about ten seconds, and asks that
  long-lived ones be replaced now and then (its "Amazon SES SMTP issues"
  page). A connection idle IDLE_SECONDS, older than MAX_AGE_SECONDS or past
  MAX_MESSAGES mails is closed and a new one opened.
- A reused connection found dropped before SES has taken anything of the
  mail -- the MAIL or RCPT command fails, or SES says it timed out -- is
  replaced and the mail tried once more on the new one. Once DATA has begun,
  a dropped connection may have delivered it: that is MailDeferred, tried
  again later as an unanswered API call is.

SES's replies (the same page's "SMTP response codes"), sorted as the API
path sorts its errors:

- 454 Throttling failure (the rate or the day's quota): MailThrottled.
- Any other 4xx (421 too many connections, 451 temporary failure or timeout,
  454 temporary authentication or service failure), a timeout, a refused or
  dropped connection: MailDeferred.
- 530, 534, 535, 538 (credentials invalid, authentication required), 554
  Access denied (the IAM user may not ses:SendRawEmail), no STARTTLS or AUTH
  offered, a certificate that does not verify: SmtpConfigError. Every mail
  would fail the same way, so it is final for the mail, said at ERROR
  (Sentry) at most once a minute, and -- when it happened while connecting --
  the same settings are not tried again for AUTH_PAUSE_SECONDS.
- Any other 5xx -- 554 Message rejected (an unverified address, the
  sandbox), 552 too long, 553 a malformed address: SmtpRejected, final for
  this mail, as the API's MessageRejected is.
"""
from __future__ import annotations

import hashlib
import logging
import re
import smtplib
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.core.email.transport import MailDeferred, MailThrottled

logger = logging.getLogger(__name__)

#: SES closes a connection idle for about ten seconds; one idle longer than
#: this is not trusted with a mail.
IDLE_SECONDS = 8.0
#: SES replaces the hosts behind its endpoint now and then; a connection
#: older than this, or that has carried this many mails, is replaced.
MAX_AGE_SECONDS = 300.0
MAX_MESSAGES = 100
#: After SES refused the settings while connecting (bad credentials, no
#: TLS), how long the same settings are not tried again.
AUTH_PAUSE_SECONDS = 60.0
#: How often a configuration error is said at ERROR (the rest at WARNING).
ALERT_EVERY_SECONDS = 60.0

#: Replies that mean the credentials (or the lack of them), not the mail.
AUTH_CODES = frozenset({530, 534, 535, 538})


class SmtpRejected(Exception):
    """SES's final word on this mail (a 5xx): not sent, and sending it again
    as it is would be refused again. The API path's MessageRejected."""

    #: The API's error code for the same refusal, for callers that read one.
    api_code = "MessageRejected"

    def __init__(self, code: int, text: str, stage: str):
        self.smtp_code = code
        self.smtp_text = text
        self.stage = stage
        # The shape of botocore's ClientError.response, so a caller that reads
        # the API's error code (the club mailbox) reads this one the same way.
        self.response = {"Error": {"Code": self.api_code, "Message": text}}
        super().__init__(self._describe())

    def _describe(self) -> str:
        return f"SES refused the mail ({self.smtp_code} {self.smtp_text})" if self.smtp_code else f"SES refused the mail ({self.smtp_text})"


class SmtpConfigError(SmtpRejected):
    """The server's SMTP settings, not the mail: every mail would be refused
    the same way until they are fixed. Never carries a credential."""

    api_code = "SmtpConfiguration"

    def _describe(self) -> str:
        said = f"{self.smtp_code} {self.smtp_text}" if self.smtp_code else self.smtp_text
        return f"SES's SMTP interface refused this server's settings ({said}); check APP_SMTP_HOST, APP_SMTP_USERNAME and APP_SMTP_PASSWORD"


class _Stale(Exception):
    """A reused connection turned out dropped before SES took any of the mail."""


# ─── Settings ────────────────────────────────────────────────────────────


def configured() -> bool:
    """Whether the SMTP interface can be used: an endpoint and credentials.
    (The sender, SES_DEFAULT_SENDER, is the API path's too.)"""
    return bool(_host() and (settings.SMTP_USERNAME or "").strip() and (settings.SMTP_PASSWORD or ""))


def _host() -> str:
    return (settings.SMTP_HOST or "").strip()


def _key() -> tuple:
    """What a connection was opened with. A change (a test, a new .env)
    opens a new one. The password is kept as a digest only."""
    password = hashlib.sha256((settings.SMTP_PASSWORD or "").encode()).hexdigest()
    return (_host(), int(settings.SMTP_PORT or 587), (settings.SMTP_USERNAME or "").strip(), password, float(settings.SMTP_TIMEOUT or 20.0))


def _tls_context() -> ssl.SSLContext:
    """Certificates checked against the system's roots, and the host name too."""
    return ssl.create_default_context()


# ─── One connection per thread ───────────────────────────────────────────


@dataclass
class _Conn:
    smtp: smtplib.SMTP
    key: tuple
    generation: int
    opened: float
    used: float
    sent: int = 0


_local = threading.local()
_state_lock = threading.Lock()
#: Bumped by reset_for_tests(): every thread then drops the connection it holds.
_generation = 0
#: (settings key, until, error) after SES refused the settings while connecting.
_refusal: Optional[tuple[tuple, float, SmtpConfigError]] = None
_last_alert = 0.0


def _now() -> float:
    return time.monotonic()


def _held() -> Optional[_Conn]:
    return getattr(_local, "conn", None)


def _discard(conn: Optional[_Conn], *, polite: bool = False) -> None:
    """Close a connection this thread holds. `polite`: say QUIT first (a live
    one being replaced); a dropped or stale one is just closed, since QUIT
    on a half-open socket would wait out the timeout."""
    if conn is None:
        return
    if getattr(_local, "conn", None) is conn:
        _local.conn = None
    try:
        if polite:
            conn.smtp.quit()
        else:
            conn.smtp.close()
    except Exception:  # noqa: BLE001 - it is being thrown away
        try:
            conn.smtp.close()
        except Exception:  # noqa: BLE001
            pass


def _connection(key: tuple) -> tuple[_Conn, bool]:
    """This thread's connection, and whether it was reused (False: just opened)."""
    conn = _held()
    if conn is not None:
        now = _now()
        if conn.key != key or conn.generation != _generation:
            _discard(conn, polite=conn.key == key)
        elif now - conn.used > IDLE_SECONDS:
            _discard(conn)
        elif now - conn.opened > MAX_AGE_SECONDS or conn.sent >= MAX_MESSAGES:
            _discard(conn, polite=True)
        else:
            return conn, True
    _check_refusal(key)
    smtp = _open(key)
    now = _now()
    conn = _Conn(smtp=smtp, key=key, generation=_generation, opened=now, used=now)
    _local.conn = conn
    return conn, False


def _check_refusal(key: tuple) -> None:
    global _refusal
    with _state_lock:
        refusal = _refusal
        if refusal is None:
            return
        if refusal[0] != key or _now() >= refusal[1]:
            _refusal = None
            return
    error = refusal[2]
    raise SmtpConfigError(error.smtp_code, error.smtp_text, error.stage)


def _refuse_for_a_while(key: tuple, error: SmtpConfigError) -> SmtpConfigError:
    global _refusal
    with _state_lock:
        _refusal = (key, _now() + AUTH_PAUSE_SECONDS, error)
    return _alert(error)


def _alert(error: SmtpConfigError) -> SmtpConfigError:
    """Say a configuration error: at ERROR (which Sentry reports) at most once
    a minute in this process, at WARNING otherwise."""
    global _last_alert
    with _state_lock:
        now = _now()
        loud = _last_alert == 0.0 or now - _last_alert >= ALERT_EVERY_SECONDS
        if loud:
            _last_alert = now
    logger.log(logging.ERROR if loud else logging.WARNING, "Mail through SES SMTP: %s", error)
    return error


def _text(reply) -> str:
    if isinstance(reply, bytes):
        reply = reply.decode("utf-8", "replace")
    return " ".join(str(reply or "").split())[:300]


def _reply_error(code: int, reply, stage: str) -> Exception:
    """The exception a refusal is, by its reply code (see the module's docstring)."""
    text = _text(reply)
    low = text.lower()
    if code == 454 and "throttl" in low:
        return MailThrottled(f"SES is throttling ({code} {text}); it will be tried again")
    if 400 <= code < 500:
        return MailDeferred(f"SES could not take it just now ({code} {text}); it will be tried again")
    if code in AUTH_CODES or (code == 554 and ("access denied" in low or "not authorized" in low)):
        return SmtpConfigError(code, text, stage)
    return SmtpRejected(code, text, stage)


def _mail_error(code: int, reply, stage: str) -> Exception:
    """A refusal of one mail's transaction; a configuration error is said too."""
    error = _reply_error(code, reply, stage)
    return _alert(error) if isinstance(error, SmtpConfigError) else error


def _open(key: tuple) -> smtplib.SMTP:
    """A new connection: greeting, EHLO, STARTTLS (verified), EHLO again, AUTH.
    Nothing secret is sent unless the socket is TLS by then."""
    host, port, username, _digest, timeout = key
    try:
        # gethostname, not smtplib's default getfqdn: a reverse lookup can
        # take seconds where DNS is slow, and SES does not read the name.
        smtp = smtplib.SMTP(host, port, local_hostname=socket.gethostname() or "localhost", timeout=timeout)
    except smtplib.SMTPConnectError as e:
        raise _connect_error(key, e.smtp_code, e.smtp_error, "connect") from e
    except (OSError, smtplib.SMTPException) as e:
        raise MailDeferred(f"SES's SMTP endpoint could not be reached ({e.__class__.__name__}); it will be tried again") from e
    try:
        code, reply = smtp.ehlo()
        if code != 250:
            raise _connect_error(key, code, reply, "EHLO")
        if not smtp.has_extn("starttls"):
            raise _refuse_for_a_while(key, SmtpConfigError(0, f"{host}:{port} does not offer STARTTLS, so the credentials were not sent", "EHLO"))
        try:
            smtp.starttls(context=_tls_context())
        except smtplib.SMTPResponseException as e:
            raise _connect_error(key, e.smtp_code, e.smtp_error, "STARTTLS") from e
        except ssl.SSLCertVerificationError as e:
            raise _refuse_for_a_while(key, SmtpConfigError(0, f"{host}'s certificate did not verify ({e.verify_message or 'untrusted'})", "STARTTLS")) from e
        if not _tls_on(smtp):
            raise _refuse_for_a_while(key, SmtpConfigError(0, "the connection is not encrypted, so the credentials were not sent", "STARTTLS"))
        code, reply = smtp.ehlo()
        if code != 250:
            raise _connect_error(key, code, reply, "EHLO")
        if not smtp.has_extn("auth"):
            raise _refuse_for_a_while(key, SmtpConfigError(0, f"{host}:{port} does not offer AUTH", "EHLO"))
        try:
            smtp.login(username, settings.SMTP_PASSWORD or "")
        except smtplib.SMTPAuthenticationError as e:
            raise _connect_error(key, e.smtp_code, e.smtp_error, "AUTH") from e
        except smtplib.SMTPException as e:
            # No mechanism both sides know: settings, not the mail.
            if isinstance(e, smtplib.SMTPServerDisconnected):
                raise
            raise _refuse_for_a_while(key, SmtpConfigError(0, f"AUTH failed ({e.__class__.__name__})", "AUTH")) from e
    except (MailDeferred, SmtpRejected):
        smtp.close()
        raise
    except (OSError, smtplib.SMTPException) as e:
        # Timed out, dropped, or the TLS handshake failed on the way: nothing was sent.
        smtp.close()
        raise MailDeferred(f"SES's SMTP endpoint did not answer ({e.__class__.__name__}); it will be tried again") from e
    except BaseException:
        smtp.close()
        raise
    return smtp


def _connect_error(key: tuple, code: int, reply, stage: str) -> Exception:
    error = _reply_error(code, reply, stage)
    if isinstance(error, SmtpConfigError):
        return _refuse_for_a_while(key, error)
    if isinstance(error, SmtpRejected):
        # A 5xx while connecting is about this server, not any one mail.
        return _refuse_for_a_while(key, SmtpConfigError(error.smtp_code, error.smtp_text, stage))
    return error


def _tls_on(smtp: smtplib.SMTP) -> bool:
    """Whether the socket is TLS now. smtplib raises when STARTTLS is refused;
    this is the belt to that: no cipher, no AUTH."""
    cipher = getattr(getattr(smtp, "sock", None), "cipher", None)
    try:
        return bool(callable(cipher) and cipher())
    except Exception:  # noqa: BLE001
        return False


# ─── Sending ─────────────────────────────────────────────────────────────

_EOL = re.compile(r"\r\n|\r|\n")


def wire_bytes(raw: str) -> bytes:
    """The raw MIME as SMTP carries it: every line ended CRLF. Nothing else
    changes (smtplib's data() adds the dot-stuffing and the final dot)."""
    return _EOL.sub("\r\n", raw).encode("utf-8")


_SES_ID = re.compile(r"^<?([A-Za-z0-9][A-Za-z0-9._-]*)>?$")


def message_id(reply) -> Optional[str]:
    """SES's message id from the reply to the end of DATA, `250 Ok <id>`."""
    words = _text(reply).split()
    for i, word in enumerate(words):
        if word.lower() == "ok" and i + 1 < len(words):
            m = _SES_ID.match(words[i + 1])
            return m.group(1) if m else None
    return None


def _reset(conn: _Conn) -> None:
    """End a refused transaction so the connection can carry the next mail;
    one that cannot be reset is thrown away."""
    try:
        code, _ = conn.smtp.rset()
        if code == 250:
            return
    except Exception:  # noqa: BLE001
        pass
    _discard(conn)


def _envelope(conn: _Conn, sender: str, recipients: list[str], reused: bool) -> None:
    """MAIL FROM and RCPT TO. Nothing of the mail has been taken yet, so a
    reused connection that turns out dropped is _Stale: try again on a new one."""
    smtp = conn.smtp
    stage = "MAIL FROM"
    try:
        code, reply = smtp.mail(sender)
        if code == 250:
            stage = "RCPT TO"
            for address in recipients:
                code, reply = smtp.rcpt(address)
                if code not in (250, 251):
                    break
    except UnicodeEncodeError as e:
        _reset(conn)
        raise SmtpRejected(0, "an address is not plain ASCII, which SES does not accept", stage) from e
    except (OSError, smtplib.SMTPServerDisconnected) as e:
        _discard(conn)
        if reused:
            raise _Stale() from e
        raise MailDeferred(f"SES's SMTP endpoint dropped the connection ({e.__class__.__name__}); it will be tried again") from e
    if code in (250, 251):
        return
    if code in (421, 451) and reused and stage == "MAIL FROM":
        # "Timeout waiting for data from client": SES had closed it.
        _discard(conn)
        raise _Stale()
    if code == 421:
        _discard(conn)
    else:
        _reset(conn)
    raise _mail_error(code, reply, stage)


def _data(conn: _Conn, payload: bytes) -> Optional[str]:
    smtp = conn.smtp
    try:
        code, reply = smtp.data(payload)
    except smtplib.SMTPDataError as e:
        # DATA itself refused (no 354): the mail was not sent.
        _reset(conn)
        raise _mail_error(e.smtp_code, e.smtp_error, "DATA") from e
    except (OSError, smtplib.SMTPServerDisconnected) as e:
        # It may have reached SES before the connection went.
        _discard(conn)
        raise MailDeferred(f"SES did not answer the mail ({e.__class__.__name__}); it will be tried again") from e
    conn.used = _now()
    if code != 250:
        if code == 421:
            _discard(conn)
        raise _mail_error(code, reply, "DATA")
    conn.sent += 1
    found = message_id(reply)
    if not found:
        # Taken, with nothing to match SES's notices by. Sending it again
        # would be a second copy, so it stands.
        logger.warning("SES took a mail over SMTP without a message id in its reply (%s)", _text(reply)[:80])
    return found


def send(sender: str, recipients: list[str], raw: str) -> Optional[str]:
    """Hand SES one mail over SMTP and return its message id. `sender` is the
    envelope's (the bare address), `recipients` every To, Cc and Bcc, `raw`
    the MIME exactly as the API path would send it. Blocking: call it off
    the event loop, as transport.deliver is."""
    if not recipients:
        raise ValueError("A mail needs at least one recipient")
    payload = wire_bytes(raw)
    key = _key()
    conn, reused = _connection(key)
    try:
        _envelope(conn, sender, recipients, reused)
    except _Stale:
        # Dropped while idle; nothing of this mail was taken. Once more, on a
        # new connection (a new one never raises _Stale).
        logger.info("SES SMTP connection had dropped; opening a new one")
        conn, _ = _connection(key)
        _envelope(conn, sender, recipients, False)
    conn.used = _now()
    return _data(conn, payload)


def reset_for_tests() -> None:
    """Forget refusals and have every thread drop the connection it holds."""
    global _generation, _refusal, _last_alert
    with _state_lock:
        _generation += 1
        _refusal = None
        _last_alert = 0.0
    _discard(_held())
