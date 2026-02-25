from pydantic import BaseModel, Field
from typing import Optional


class TicketTemplateConfig(BaseModel):
    accent_color: str = Field(default="#1db9a0", description="Outer border / decorative color")
    bg_color: str = Field(default="#121212", description="Inner ticket background")
    text_color: str = Field(default="#ffffff", description="Primary text color")
    font: str = Field(default="Inter", description="Google Font name")
    logo_url: Optional[str] = Field(default=None, description="Club logo URL for ticket")
    background_image_url: Optional[str] = Field(default=None, description="Optional background image")
    show_prizes: bool = Field(default=True, description="Show prize info on ticket")
    show_guidelines_link: bool = Field(default=True, description="Show guidelines link")
    ticket_type_label: str = Field(default="GENERAL", description="Ticket type label like VIP, EARLY BIRD")
    custom_footer_text: Optional[str] = Field(default=None, description="Custom text at bottom of ticket")


class TicketTemplateCreate(BaseModel):
    name: str = Field(..., max_length=100)
    is_default: bool = False
    config: TicketTemplateConfig = TicketTemplateConfig()


class TicketTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    is_default: Optional[bool] = None
    config: Optional[TicketTemplateConfig] = None


class TicketTemplateResponse(BaseModel):
    id: int
    name: str
    is_default: bool
    config: dict

    class Config:
        from_attributes = True
