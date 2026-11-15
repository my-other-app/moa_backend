"""The API's own docs and route map (app/api/api_docs.py): /docs, /redoc,
/openapi.json. Served in development and in the suite; in production a 404
like any unknown path, but for /openapi.json to the website's own server (a
valid X-Site-Token). The partner API's document keeps its own gate."""
import pytest

from app.api import api_docs
from app.config import settings

DOCS = ("/docs", "/redoc", "/docs/oauth2-redirect", "/openapi.json")
SITE_TOKEN = "s" * 40


@pytest.fixture
def production(monkeypatch):
    """This process as production sees it: no ENV, DEBUG off, APP_API_DOCS unset."""
    monkeypatch.setattr(settings, "ENV", "")
    monkeypatch.setattr(settings, "DEBUG", False)
    monkeypatch.setattr(settings, "API_DOCS", None)
    assert not api_docs.enabled()


async def test_the_docs_are_served_in_the_suite(client):
    assert api_docs.enabled()
    for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
        r = await client.get(path)
        assert r.status_code == 200 and "text/html" in r.headers["content-type"], path
    assert "/openapi.json" in (await client.get("/docs")).text
    doc = (await client.get("/openapi.json")).json()
    assert doc["openapi"].startswith("3.") and "/api/v1/auth/google" in doc["paths"]
    # The docs' own routes are not in the route map.
    assert not {"/docs", "/redoc", "/openapi.json"} & set(doc["paths"])


async def test_production_answers_404_like_an_unknown_path(client, production):
    unknown = await client.get("/no-such-page")
    assert unknown.status_code == 404
    for path in DOCS:
        r = await client.get(path)
        assert r.status_code == 404 and r.json() == unknown.json(), path


async def test_staging_is_production_for_the_docs(client, monkeypatch):
    monkeypatch.setattr(settings, "ENV", "staging")
    monkeypatch.setattr(settings, "API_DOCS", None)
    assert (await client.get("/openapi.json")).status_code == 404


async def test_the_site_token_still_gets_the_schema(client, production, monkeypatch):
    monkeypatch.setattr(settings, "SITE_TOKENS", SITE_TOKEN)
    r = await client.get("/openapi.json", headers={"X-Site-Token": SITE_TOKEN})
    assert r.status_code == 200 and "/api/v1/auth/google" in r.json()["paths"]
    # Only the schema: the pages stay closed, and a wrong token is anyone.
    assert (await client.get("/docs", headers={"X-Site-Token": SITE_TOKEN})).status_code == 404
    assert (await client.get("/openapi.json", headers={"X-Site-Token": "t" * 40})).status_code == 404


async def test_the_setting_says_so_outright(client, production, monkeypatch):
    monkeypatch.setattr(settings, "API_DOCS", True)
    assert (await client.get("/openapi.json")).status_code == 200
    monkeypatch.setattr(settings, "ENV", "test")
    monkeypatch.setattr(settings, "API_DOCS", False)
    assert (await client.get("/docs")).status_code == 404


async def test_the_partner_document_keeps_its_own_gate_in_production(client, production):
    """Not this switch: without a partner credential it is refused as before (401), not hidden."""
    assert (await client.get("/partner/v1/openapi.json")).status_code == 401
