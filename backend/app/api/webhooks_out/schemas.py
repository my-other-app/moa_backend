"""What the endpoints' routes take (both surfaces). Answers are plain
dicts made by service.endpoint_out / delivery_out, the same on both."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class EndpointIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., max_length=2000, description="https only; its host must resolve to public addresses")
    events: list[str] = Field(default_factory=lambda: ["*"], description='Types to send, or ["*"] for all')
    description: Optional[str] = Field(None, max_length=200)
    include_own_changes: bool = Field(False, description="Also send the changes this endpoint's own client made")
    livemode: Optional[bool] = Field(None, description="Live changes (true) or test-mode ones (false); an API key's own mode")
    share_attendee_details: bool = Field(False, description="Send people's names, emails, phones and answers (club endpoints)")
    dpdp_confirmed: bool = Field(False, description="The club's registration form tells people their details are shared with this endpoint's system")
    client_id: Optional[str] = Field(None, max_length=120, description="Dashboard only: the API key whose own changes are not sent back")


class EndpointPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: Optional[str] = Field(None, max_length=2000)
    events: Optional[list[str]] = None
    description: Optional[str] = Field(None, max_length=200)
    include_own_changes: Optional[bool] = None
    enabled: Optional[bool] = None
    share_attendee_details: Optional[bool] = None
    dpdp_confirmed: bool = False
    client_id: Optional[str] = Field(None, max_length=120)


class RotateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keep_previous_hours: Optional[int] = Field(None, ge=0, le=72, description="How long the old secret still signs too (default 24)")


class AdminEndpointPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
