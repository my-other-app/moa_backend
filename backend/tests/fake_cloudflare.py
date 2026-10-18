"""An in-memory Cloudflare for SaaS custom hostnames API, for the tests.

`install(monkeypatch)` turns the feature on (a token and a zone in the
settings), points the client at this fake through httpx's MockTransport, and
returns the fake. Nothing reaches the network: a request the fake does not
know fails the test.

The fake behaves like Cloudflare where the backend depends on it: a new
hostname is pending with a TXT ownership record and, unless told otherwise,
its certificate's TXT record; `go_live` is what adding the records does;
DELETE of an unknown id is a 404; a hostname made twice is error 1406.
"""
from __future__ import annotations

import json
import re
import uuid

import httpx

from app.api.club_domains import cloudflare as cf
from app.api.club_domains import service
from app.config import settings

TOKEN = "test-cf-token"
ZONE = "zone-test"
_PATH = re.compile(rf"^/client/v4/zones/{ZONE}/custom_hostnames(?:/(?P<id>[^/]+))?$")


class FakeCloudflare:
    def __init__(self):
        self.hostnames: dict[str, dict] = {}
        self.calls: list[tuple[str, str, dict | None]] = []
        #: The next request answers this error: (http status, code, message).
        self.fail_next: tuple[int, int, str] | None = None
        #: Every request fails as if Cloudflare could not be reached.
        self.down = False
        #: Whether a new hostname's certificate TXT record is there at once.
        self.ssl_records_at_once = True

    # ── What the tests do to it ─────────────────────────────────────────

    def by_host(self, host: str) -> dict | None:
        return next((h for h in self.hostnames.values() if h["hostname"] == host), None)

    def add(self, host: str) -> dict:
        """A hostname made at Cloudflare without us (a delete that failed)."""
        return self._make(host)

    def go_live(self, host: str) -> None:
        h = self.by_host(host)
        h["status"] = "active"
        h["verification_errors"] = []
        h["ssl"]["status"] = "active"
        h["ssl"]["validation_records"] = []
        h["ssl"]["validation_errors"] = []

    def set(self, host: str, *, status: str | None = None, ssl_status: str | None = None, errors=None) -> None:
        h = self.by_host(host)
        if status:
            h["status"] = status
        if ssl_status:
            h["ssl"]["status"] = ssl_status
        if errors is not None:
            h["verification_errors"] = list(errors)

    def drop(self, host: str) -> None:
        h = self.by_host(host)
        self.hostnames.pop(h["id"], None)

    def methods(self) -> list[str]:
        return [m for m, _, _ in self.calls]

    # ── The API ─────────────────────────────────────────────────────────

    def _make(self, host: str) -> dict:
        hid = uuid.uuid4().hex
        row = {
            "id": hid,
            "hostname": host,
            "status": "pending",
            "verification_errors": ["custom hostname does not CNAME to this zone."],
            "ownership_verification": {"type": "txt", "name": f"_cf-custom-hostname.{host}", "value": f"own-{hid[:12]}"},
            "ssl": {
                "id": f"ssl-{hid[:8]}", "type": "dv", "method": "txt",
                "status": "pending_validation" if self.ssl_records_at_once else "initializing",
                "validation_records": (
                    [{"txt_name": f"_acme-challenge.{host}", "txt_value": f"acme-{hid[:16]}", "status": "pending"}]
                    if self.ssl_records_at_once else []
                ),
                "validation_errors": [],
            },
            "created_at": "2026-10-18T00:00:00Z",
        }
        self.hostnames[hid] = row
        return row

    @staticmethod
    def _ok(result, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json={"success": True, "errors": [], "messages": [], "result": result})

    @staticmethod
    def _err(status: int, code: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"success": False, "errors": [{"code": code, "message": message}], "messages": [], "result": None})

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {TOKEN}", "the token goes as a bearer header"
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if self.down:
            raise httpx.ConnectError("unreachable", request=request)
        if self.fail_next:
            status, code, message = self.fail_next
            self.fail_next = None
            return self._err(status, code, message)
        m = _PATH.match(request.url.path)
        assert m, f"the fake Cloudflare does not know {request.method} {request.url.path}"
        hid = m.group("id")
        if hid is None and request.method == "POST":
            assert body["ssl"]["method"] == "txt" and body["ssl"]["type"] == "dv"
            if self.by_host(body["hostname"]):
                return self._err(409, 1406, "Duplicate custom hostname found.")
            row = self._make(body["hostname"])
            if not self.ssl_records_at_once:
                # Cloudflare fills the record in a moment later.
                self.ssl_records_at_once = True
                answer = json.loads(json.dumps(row))
                row["ssl"]["status"] = "pending_validation"
                row["ssl"]["validation_records"] = [{"txt_name": f"_acme-challenge.{row['hostname']}", "txt_value": f"acme-{row['id'][:16]}"}]
                return self._ok(answer, 201)
            return self._ok(row, 201)
        if hid is None and request.method == "GET":
            host = request.url.params.get("hostname")
            rows = [h for h in self.hostnames.values() if host is None or h["hostname"] == host]
            return self._ok(rows)
        row = self.hostnames.get(hid)
        if row is None:
            return self._err(404, 1436, "The custom hostname was not found.")
        if request.method == "GET":
            return self._ok(row)
        if request.method == "PATCH":
            assert body == {"ssl": cf.SSL}
            return self._ok(row)
        if request.method == "DELETE":
            self.hostnames.pop(hid)
            return self._ok({"id": hid})
        raise AssertionError(f"unexpected {request.method}")


def install(monkeypatch) -> FakeCloudflare:
    fake = FakeCloudflare()
    monkeypatch.setattr(settings, "CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "CLOUDFLARE_ZONE_ID", ZONE)
    monkeypatch.setattr(service, "SSL_RECORDS_WAIT", 0)
    monkeypatch.setattr(cf, "TRANSPORT", httpx.MockTransport(fake.handler))
    return fake
