from pydantic import BaseModel, Field
from typing import Optional


class CertificateTemplateConfig(BaseModel):
    accent_color: str = Field(default="#1db9a0", description="Primary decorative color")
    bg_color: str = Field(default="#ffffff", description="Certificate background color")
    text_color: str = Field(default="#000000", description="Primary text color")
    font: str = Field(default="Inter", description="Google Font name")
    logo_url: Optional[str] = Field(default=None, description="Club logo URL for certificate")
    background_image_url: Optional[str] = Field(default=None, description="Optional background image")
    signature_url: Optional[str] = Field(default=None, description="Optional signature image URL")
    custom_text: Optional[str] = Field(default=None, description="Custom text to display on certificate")


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

    class Config:
        from_attributes = True
