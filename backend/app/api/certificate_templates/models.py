from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    JSON,
)
from sqlalchemy.orm import relationship

from app.db.base import AbstractSQLModel
from app.db.mixins import SoftDeleteMixin, TimestampsMixin
from app.core.storage.fields import S3ImageField


class CertificateTemplates(AbstractSQLModel, TimestampsMixin, SoftDeleteMixin):
    __tablename__ = "certificate_templates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    club_id = Column(Integer, ForeignKey("clubs.id"), nullable=False)
    name = Column(String(100), nullable=False)
    is_default = Column(Boolean, default=False, nullable=False)

    # Uploaded images (S3)
    logo = Column(
        S3ImageField(upload_to="templates/certificates/logos/"),
        nullable=True,
    )
    secondary_logo = Column(
        S3ImageField(upload_to="templates/certificates/logos/"),
        nullable=True,
    )
    background_image = Column(
        S3ImageField(upload_to="templates/certificates/backgrounds/"),
        nullable=True,
    )
    signature_1 = Column(
        S3ImageField(upload_to="templates/certificates/signatures/"),
        nullable=True,
    )
    signature_2 = Column(
        S3ImageField(upload_to="templates/certificates/signatures/"),
        nullable=True,
    )

    # Style configuration JSON (all non-file options)
    config = Column(JSON, nullable=False, default=dict)

    club = relationship("Clubs")
