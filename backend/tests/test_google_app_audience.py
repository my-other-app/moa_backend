"""POST /auth/google from the app: any of our own Google clients is an
accepted audience, whichever platform the app names.

The app sends platform "web" on both phones (its serverClientId is the web
client). Android's token then has the web client as its audience, but an
iPhone's has the iOS client, so checking only the named platform's client
refused every iPhone sign-in as INVALID_GOOGLE_TOKEN. A client that isn't
ours is still refused.

The tokens are real RS256 JWTs signed with the suite's key, so google-auth's
own checks run on them (see test_dashboard_google)."""
import pytest

from app.api.auth import service as auth_service
from app.config import settings
from app.core.rate_limit import limiter
from tests.test_dashboard_google import _Certs, google_token

URL = "/api/v1/auth/google"


@pytest.fixture(autouse=True)
def google(monkeypatch):
    monkeypatch.setattr(auth_service, "_google_certs", _Certs())
    monkeypatch.setattr(limiter, "enabled", False)


@pytest.mark.parametrize("platform", ["web", "ios", "android"])
async def test_an_iphone_token_signs_in_whatever_platform_the_app_names(client, platform):
    token = google_token("iphone@example.com", aud=settings.GOOGLE_IOS_CLIENT_ID)

    response = await client.post(URL, json={"id_token": token, "platform": platform})

    assert response.status_code == 200, response.json()
    assert response.json()["access_token"]


async def test_an_android_token_with_the_web_audience_still_signs_in(client):
    token = google_token("android@example.com", aud=settings.GOOGLE_WEB_CLIENT_ID)

    response = await client.post(URL, json={"id_token": token, "platform": "web"})

    assert response.status_code == 200, response.json()


async def test_a_token_for_someone_elses_client_is_refused(client):
    token = google_token("stranger@example.com", aud="999-other-app.apps.googleusercontent.com")

    response = await client.post(URL, json={"id_token": token, "platform": "web"})

    assert response.status_code == 401
    assert response.json()["error_code"] == "INVALID_GOOGLE_TOKEN"


async def test_an_unknown_platform_is_still_refused(client):
    token = google_token("iphone@example.com", aud=settings.GOOGLE_IOS_CLIENT_ID)

    response = await client.post(URL, json={"id_token": token, "platform": "windows"})

    assert response.status_code == 400
    assert response.json()["error_code"] == "INVALID_PLATFORM"
