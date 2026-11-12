from typing import Optional

from pydantic import Field

from app.core.response.base_model import CustomBaseModel


class SlugIn(CustomBaseModel):
    #: As typed; lower-cased by the server, then 3-60 of [a-z0-9-] (422 SLUG_INVALID).
    slug: str = Field(..., max_length=200)


class SlugOut(CustomBaseModel):
    #: The slug now.
    slug: str
    #: The old ones that still answer (and redirect), the most recent first.
    aliases: list[str]
    #: The public page: myotherapp.in/events/{slug} or myotherapp.in/clubs/{slug}.
    url: str
    #: A club's website at its subdomain (https://{slug}.myotherapp.in); null for an event.
    site_url: Optional[str] = None
