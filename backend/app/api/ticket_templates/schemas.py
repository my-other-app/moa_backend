from pydantic import BaseModel, Field
from typing import Optional


class TicketTemplateConfig(BaseModel):
    # Colors
    accent_color: str = Field(default="#1db9a0", description="Outer border / decorative color")
    bg_color: str = Field(default="#121212", description="Inner ticket background")
    text_color: str = Field(default="#ffffff", description="Primary text color")
    secondary_text_color: str = Field(default="#aaaaaa", description="Secondary/muted text color")
    border_color: str = Field(default="#1db9a0", description="Border color")

    # Typography
    font: str = Field(default="Inter", description="Google Font name")

    # Layout & Options
    ticket_type_label: str = Field(default="GENERAL", description="GENERAL, VIP, EARLY BIRD, PREMIUM, STUDENT, FREE")
    border_style: str = Field(default="none", description="none, solid, dashed, double")
    show_prizes: bool = Field(default=True, description="Show prize info on ticket")
    show_guidelines_link: bool = Field(default=True, description="Show guidelines link")

    # Content
    custom_header_text: Optional[str] = Field(default=None, description="Custom text above event name")
    organizer_subtitle: Optional[str] = Field(default=None, description="e.g. 'Presents' or 'In association with'")
    custom_footer_text: Optional[str] = Field(default=None, description="Custom text at bottom of ticket")

    # Image URLs — auto-populated from S3 uploads, not user-editable
    logo_url: Optional[str] = Field(default=None, description="Auto-populated from logo upload")
    secondary_logo_url: Optional[str] = Field(default=None, description="Auto-populated from secondary_logo upload")
    background_image_url: Optional[str] = Field(default=None, description="Auto-populated from background_image upload")


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
    logo: Optional[dict] = None
    secondary_logo: Optional[dict] = None
    background_image: Optional[dict] = None

    class Config:
        from_attributes = True
