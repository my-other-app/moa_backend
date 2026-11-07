"""Addresses on the public site, made in one place so moving the site is one setting (PUBLIC_SITE_URL)."""
from app.config import settings


def site_url(path: str = "") -> str:
    return settings.PUBLIC_SITE_URL.rstrip("/") + path


def event_url(slug) -> str:
    """An event's page. By slug: the old site's /{id} addresses are gone."""
    return site_url(f"/events/{slug}")


def ticket_url(ticket_id: str) -> str:
    return site_url(f"/tickets/{ticket_id}")


def certificate_url(public_id: str) -> str:
    """A certificate's public page, by the certificate's own public id
    (EventRegistrationsLink.certificate_public_id) -- never the ticket id,
    which is what the door scans."""
    return site_url(f"/certificates/{public_id}")
