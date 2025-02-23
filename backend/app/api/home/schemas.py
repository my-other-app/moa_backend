from pydantic import BaseModel

from app.api.clubs.schemas import ClubPublicMin, NotesPublic
from app.api.events.schemas import EventPublicMin


class SearchResults(BaseModel):
    events: list[EventPublicMin]
    clubs: list[ClubPublicMin]
    notes: list[NotesPublic]
