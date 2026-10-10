"""Who a ticket or certificate design is for.

An event issues the same kind of document to people in different roles, and
they are not the same document: a speaker's badge says SPEAKER on it, staff
need theirs to read differently at a glance on the door, and an attendee's
is the ordinary one.

`event_default` is the design used when a role has none of its own, which is
most of the time. It is not a role anybody holds -- a registration is always
one of the other three -- so resolution falls back to it rather than
matching it.
"""

import enum


class TemplateAudience(enum.Enum):
    """The variant a design belongs to."""

    event_default = "event_default"
    attendee = "attendee"
    speaker = "speaker"
    staff = "staff"


class RegistrationRole(enum.Enum):
    """What a registration is, which picks the variant their document uses.

    Separate from TemplateAudience on purpose: a person is never
    `event_default`, and a design never belongs to "whoever is left".
    Keeping them apart stops the fallback value leaking into the attendee
    list as though it were a role.
    """

    attendee = "attendee"
    speaker = "speaker"
    staff = "staff"

    @property
    def audience(self) -> TemplateAudience:
        """The design variant this role looks for first."""
        return TemplateAudience(self.value)
