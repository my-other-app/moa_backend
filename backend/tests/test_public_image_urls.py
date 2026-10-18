"""Links to pictures: a public picture (what the site, the app and emails show
of an event or a club) is signed for a week and handed out as the same link
for half of that; a private file keeps its hour. Signing is local work, so
nothing here reaches S3; the signer is watched rather than replaced."""
import pytest

from app.api.agenda import service as agenda
from app.api.agenda.models import AgendaItems
from app.api.certificate_templates.models import CertificateTemplates
from app.api.clubs.models import ClubPartners, Clubs, ClubTeamMembers
from app.api.events.models import Events, EventSpeakers
from app.api.orgs.models import Organizations
from app.api.threads.models import ClubThreads
from app.api.ticket_templates.models import TicketTemplates
from app.api.users.models import UserAvatars, UserProfiles
from app.config import AppConfig, settings
from app.core.storage import fields

WEEK = 7 * 24 * 3600
HOUR = 3600


@pytest.fixture
def signer(monkeypatch):
    """Every signing: (key, seconds it is valid for)."""
    signed = []
    real = fields._SIGNER.generate_presigned_url

    def spy(operation, Params=None, ExpiresIn=3600, **kwargs):
        signed.append((Params["Key"], ExpiresIn))
        return real(operation, Params=Params, ExpiresIn=ExpiresIn, **kwargs)

    monkeypatch.setattr(fields._SIGNER, "generate_presigned_url", spy)
    fields._signed.clear()
    yield signed
    fields._signed.clear()


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(fields.time, "monotonic", lambda: now["t"])
    return now


def _type(model, column):
    return model.__table__.c[column].type


PUBLIC = [
    (Events, "poster"), (EventSpeakers, "photo"),
    (Clubs, "logo"), (Clubs, "cover"), (ClubTeamMembers, "photo"), (ClubPartners, "logo"),
    (ClubThreads, "image"),
]
PRIVATE = [
    (TicketTemplates, "logo"), (TicketTemplates, "background_image"),
    (CertificateTemplates, "signature_1"), (CertificateTemplates, "background_image"),
    (UserProfiles, "profile_pic"), (UserAvatars, "image"), (Organizations, "logo"),
]


def test_which_pictures_are_public():
    for model, column in PUBLIC:
        assert _type(model, column).public is True, (model.__name__, column)
    for model, column in PRIVATE:
        assert _type(model, column).public is False, (model.__name__, column)


@pytest.mark.parametrize("model, column", PUBLIC)
def test_a_public_picture_is_signed_for_a_week_every_size_of_it(signer, model, column):
    image = _type(model, column).process_result_value("media/x/abc.png", None)
    assert image["original"]
    assert signer and {seconds for _, seconds in signer} == {WEEK}
    assert len(signer) == len(image)  # each size signed once


@pytest.mark.parametrize("model, column", PRIVATE)
def test_a_private_file_keeps_its_hour(signer, model, column):
    _type(model, column).process_result_value("media/x/abc.png", None)
    assert signer and {seconds for _, seconds in signer} == {HOUR}


def test_a_public_link_stays_the_same_for_half_a_week(signer, clock):
    first = fields.signed_url("media/clubs/logos/a.png", public=True)
    clock["t"] += 3 * 24 * 3600
    assert fields.signed_url("media/clubs/logos/a.png", public=True) == first
    assert len(signer) == 1
    # Past half its life it is signed again, so whoever gets it still has days.
    clock["t"] += 12 * 3600 + 1
    fields.signed_url("media/clubs/logos/a.png", public=True)
    assert len(signer) == 2


def test_a_private_link_is_reused_for_half_an_hour_as_before(signer, clock):
    first = fields.signed_url("media/templates/t.png")
    clock["t"] += 29 * 60
    assert fields.signed_url("media/templates/t.png") == first
    clock["t"] += 2 * 60
    fields.signed_url("media/templates/t.png")
    assert [s for _, s in signer] == [HOUR, HOUR]


def test_one_key_signed_both_ways_never_mixes(signer):
    fields.signed_url("media/k.png")
    fields.signed_url("media/k.png", public=True)
    assert [s for _, s in signer] == [HOUR, WEEK]


def test_the_life_follows_the_setting(signer, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_IMAGE_URL_TTL", 2 * 24 * 3600)
    fields.signed_url("media/k.png", public=True)
    assert signer == [("media/k.png", 2 * 24 * 3600)]


@pytest.mark.parametrize("given, kept", [(WEEK, WEEK), (30 * 24 * 3600, WEEK), (WEEK + 1, WEEK), (60, HOUR), (-5, HOUR), (86400, 86400)])
def test_the_setting_stays_within_what_sigv4_signs(given, kept):
    assert AppConfig(PUBLIC_IMAGE_URL_TTL=given).PUBLIC_IMAGE_URL_TTL == kept


def test_an_agenda_speaker_photo_is_a_public_link(signer):
    row = AgendaItems(id=None, event_id=1, title="Keynote", speaker="Dr. Rao", speaker_photo_key="media/agenda/rao.png")
    out = agenda.item_out(row, set())
    assert out["speaker_photo_url"]
    assert signer == [("media/agenda/rao.png", WEEK)]
