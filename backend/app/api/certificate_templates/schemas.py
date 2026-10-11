from pydantic import BaseModel, Field
from typing import Optional

from app.api.templates_shared import LayerCanvas, TemplateLayer


class CertificateTemplateConfig(BaseModel):
    # Colors
    accent_color: str = Field(default="#1db9a0", description="Primary decorative color")
    bg_color: str = Field(default="#121212", description="Certificate background color")
    text_color: str = Field(default="#ffffff", description="Primary text color")
    secondary_text_color: str = Field(default="#aaaaaa", description="Secondary/muted text color")
    border_color: str = Field(default="#1db9a0", description="Border color")

    # Typography
    font: str = Field(default="Inter", description="Body font (Google Fonts)")
    title_font: Optional[str] = Field(default=None, description="Separate heading font (falls back to font)")

    # Content
    certificate_title: str = Field(default="Certificate of Participation", description="Main heading")
    custom_body_text: Optional[str] = Field(default=None, description="Custom description text")
    custom_footer_text: Optional[str] = Field(default=None, description="Custom footer text")

    # Signatures (names/titles — images come from S3 uploads)
    signature_1_name: Optional[str] = Field(default=None, description="Primary signatory name")
    signature_1_title: Optional[str] = Field(default=None, description="Primary signatory title")
    signature_2_name: Optional[str] = Field(default=None, description="Secondary signatory name")
    signature_2_title: Optional[str] = Field(default=None, description="Secondary signatory title")

    # Layout
    orientation: str = Field(default="landscape", description="landscape or portrait")
    border_style: str = Field(default="none", description="none, solid, dashed, double")
    show_qr_code: bool = Field(default=True, description="Show verification QR code")
    show_certificate_id: bool = Field(default=True, description="Show certificate ID")
    date_format: str = Field(default="dd MMM yyyy", description="dd MMM yyyy, MMMM dd yyyy, dd/MM/yyyy")

    # Layer mode. Empty means the classic styled layout, which is what every
    # existing template uses -- the renderer only switches when this is set.
    layers: list[TemplateLayer] = Field(default_factory=list)
    canvas: LayerCanvas = Field(default_factory=LayerCanvas)

    # The layered design, in the editor's wire form: {canvas, layers, page,
    # version}. Validated on its own endpoint through the layout schema and
    # layout_problems, not here -- this field only has to survive a config
    # save that touched a colour. When present it is what the renderer draws;
    # `layers` above is the older, flat layer format, kept so designs saved
    # with it still print.
    layout: Optional[dict] = Field(default=None)

    # Image URLs — auto-populated from S3 uploads
    logo_url: Optional[str] = Field(default=None, description="Auto-populated from logo upload")
    secondary_logo_url: Optional[str] = Field(default=None, description="Auto-populated from secondary_logo upload")
    background_image_url: Optional[str] = Field(default=None, description="Auto-populated from background_image upload")
    signature_1_url: Optional[str] = Field(default=None, description="Auto-populated from signature_1 upload")
    signature_2_url: Optional[str] = Field(default=None, description="Auto-populated from signature_2 upload")


class CertificateTemplateCreate(BaseModel):
    name: str = Field(..., max_length=100)
    is_default: bool = False
    config: CertificateTemplateConfig = CertificateTemplateConfig()


class CertificateTemplateUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    is_default: Optional[bool] = None
    config: Optional[CertificateTemplateConfig] = None


class CertificateTemplateResponse(BaseModel):
    id: int
    name: str
    is_default: bool
    config: dict
    logo: Optional[dict] = None
    secondary_logo: Optional[dict] = None
    background_image: Optional[dict] = None
    signature_1: Optional[dict] = None
    signature_2: Optional[dict] = None

    class Config:
        from_attributes = True
