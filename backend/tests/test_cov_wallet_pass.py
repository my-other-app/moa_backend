"""The Apple Wallet pass for a ticket (GET /events/tickets/{id}/wallet.pkpass):
what is inside it, that the manifest covers every file, and that the
signature is a real PKCS#7 signature over the manifest by the pass
certificate, chained to the WWDR certificate -- made here with a stand-in
WWDR authority, signed by the same openssl call production makes."""
import hashlib
import io
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from PIL import Image

from app.api.events import wallet_service
from tests.conftest import auth_header

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="signing a pass needs openssl")

START = datetime(2026, 12, 5, 13, 30, tzinfo=timezone.utc)


def _cert(subject: str, key, issuer_name: x509.Name, issuer_key, *, ca: bool) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    """A WWDR stand-in, and a pass certificate and key it issued."""
    folder = tmp_path_factory.mktemp("wallet")
    wwdr_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wwdr_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test WWDR")])
    wwdr = _cert("Test WWDR", wwdr_key, wwdr_name, wwdr_key, ca=True)
    pass_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pass_cert = _cert("Pass Type ID: pass.app.test", pass_key, wwdr_name, wwdr_key, ca=False)
    paths = dict(cert=folder / "pass_certificate_new.pem", key=folder / "pass_key_new.pem", wwdr=folder / "wwdr.pem")
    paths["cert"].write_bytes(pass_cert.public_bytes(serialization.Encoding.PEM))
    paths["key"].write_bytes(pass_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    paths["wwdr"].write_bytes(wwdr.public_bytes(serialization.Encoding.PEM))
    return paths


@pytest.fixture
def signer(certs, monkeypatch):
    monkeypatch.setattr(wallet_service, "CERT_PATH", certs["cert"])
    monkeypatch.setattr(wallet_service, "KEY_PATH", certs["key"])
    monkeypatch.setattr(wallet_service, "WWDR_PATH", certs["wwdr"])
    return certs


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Wallet Club", email="cov-wallet@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night 05", event_datetime=START, location_name="Lab 3, COEP")
    guest = await make_user(email="cov-wallet-guest@example.com")
    stranger = await make_user(email="cov-wallet-stranger@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Asha Rao")
    await session.commit()
    return dict(event=event, reg=reg, h=auth_header(await mint_token(guest)), stranger=auth_header(await mint_token(stranger)))


def _open(body: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


async def test_the_pass_carries_the_ticket_and_is_signed_over_its_manifest(client, world, signer, tmp_path):
    ticket = world["reg"].ticket_id
    r = await client.get(f"/api/v1/events/tickets/{ticket}/wallet.pkpass", headers=world["h"])
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/vnd.apple.pkpass"
    assert r.headers["content-disposition"] == f'attachment; filename="ticket-{ticket}.pkpass"'
    files = _open(r.content)
    assert {"pass.json", "manifest.json", "signature", "icon.png", "icon@2x.png", "logo.png"} <= set(files)

    p = json.loads(files["pass.json"])
    assert (p["formatVersion"], p["serialNumber"], p["passTypeIdentifier"]) == (1, ticket, wallet_service.PASS_TYPE_ID)
    assert p["barcodes"] == [{"message": ticket, "format": "PKBarcodeFormatQR", "messageEncoding": "iso-8859-1"}]
    fields = {f["key"]: f["value"] for part in p["eventTicket"].values() for f in part}
    assert fields["event"] == "Hack Night 05" and fields["location"] == "Lab 3, COEP" and fields["holder"] == "Asha Rao" and fields["ticketId"] == ticket
    assert (fields["date"], fields["time"]) == ("Dec 05, 2026", "07:00 PM")  # India's time: 13:30 UTC
    assert datetime.fromisoformat(p["relevantDate"]) == START

    # The manifest names every other file by its SHA-1.
    manifest = json.loads(files["manifest.json"])
    assert set(manifest) == set(files) - {"manifest.json", "signature"}
    assert all(manifest[name] == hashlib.sha1(files[name]).hexdigest() for name in manifest)

    # The signature is the pass certificate's, over the manifest, chained to WWDR.
    (tmp_path / "manifest.json").write_bytes(files["manifest.json"])
    (tmp_path / "signature").write_bytes(files["signature"])
    check = subprocess.run(
        ["openssl", "smime", "-verify", "-binary", "-inform", "DER", "-in", str(tmp_path / "signature"), "-content", str(tmp_path / "manifest.json"),
         "-CAfile", str(signer["wwdr"]), "-purpose", "any", "-out", "/dev/null"],
        capture_output=True,
    )
    assert check.returncode == 0, check.stderr.decode()
    # A changed manifest no longer matches it.
    (tmp_path / "manifest.json").write_bytes(files["manifest.json"].replace(b"pass.json", b"pass.jsoN"))
    tampered = subprocess.run(
        ["openssl", "smime", "-verify", "-binary", "-inform", "DER", "-in", str(tmp_path / "signature"), "-content", str(tmp_path / "manifest.json"),
         "-CAfile", str(signer["wwdr"]), "-purpose", "any", "-out", "/dev/null"],
        capture_output=True,
    )
    assert tampered.returncode != 0


async def test_the_poster_becomes_the_strip_at_most_1125_wide(client, session, world, signer, monkeypatch):
    big = io.BytesIO()
    Image.new("RGB", (2250, 900), (200, 30, 30)).save(big, "JPEG")
    real = wallet_service.get_registration_by_ticket_id
    asked = []

    async def with_poster(session, ticket_id):
        reg, event = await real(session, ticket_id)
        # The event as read, with a poster on it (S3Image is dict-like: its sizes by name).
        return reg, SimpleNamespace(name=event.name, event_datetime=event.event_datetime, location_name=event.location_name,
                                    poster={"medium": "https://test-bucket.s3.amazonaws.com/events/poster-medium.jpg"})

    monkeypatch.setattr(wallet_service, "get_registration_by_ticket_id", with_poster)
    monkeypatch.setattr(wallet_service, "download_image", lambda url: asked.append(url) or big.getvalue())
    r = await client.get(f"/api/v1/events/tickets/{world['reg'].ticket_id}/wallet.pkpass", headers=world["h"])
    assert r.status_code == 200, r.text
    files = _open(r.content)
    with Image.open(io.BytesIO(files["strip.png"])) as strip:
        assert strip.format == "PNG" and strip.size == (1125, 450)
    assert json.loads(files["manifest.json"])["strip.png"] == hashlib.sha1(files["strip.png"]).hexdigest()
    assert asked == ["https://test-bucket.s3.amazonaws.com/events/poster-medium.jpg"]  # the medium size


async def test_only_the_tickets_holder_gets_its_pass(client, world, signer):
    url = f"/api/v1/events/tickets/{world['reg'].ticket_id}/wallet.pkpass"
    assert (await client.get(url, headers=world["stranger"])).status_code == 403
    assert (await client.get(url)).status_code == 401
    assert (await client.get("/api/v1/events/tickets/MOA-NOPE0-00000/wallet.pkpass", headers=world["h"])).status_code == 404


async def test_without_the_certificates_the_server_says_so(client, world, monkeypatch, tmp_path):
    monkeypatch.setattr(wallet_service, "CERT_PATH", tmp_path / "missing.pem")
    r = await client.get(f"/api/v1/events/tickets/{world['reg'].ticket_id}/wallet.pkpass", headers=world["h"])
    assert r.status_code == 500 and "certificates" in r.json()["message"]
