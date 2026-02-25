from pydantic import BaseModel, Field
from typing import Optional


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
    logo: Optional[str] = None
    secondary_logo: Optional[str] = None
    background_image: Optional[str] = None
    signature_1: Optional[str] = None
    signature_2: Optional[str] = None

    class Config:
        from_attributes = True
