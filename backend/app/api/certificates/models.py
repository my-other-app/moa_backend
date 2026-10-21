"""The certificate files handed out, by their SHA-256.

Every certificate PDF a guest was given -- downloaded, emailed, opened
from its link, or in the organiser's bundle -- is the copy kept in the
bucket under docs/certificate/{registration}/ (app.api.events.doc_cache):
each is drawn once and handed out from there. A row here is one of those
copies, hashed, so the public verifier can say whether a file someone
holds is exactly one of them without the file leaving their browser.

The rows are filled the first time a certificate's files are asked for
(certificates.service.file_digests), from what the bucket holds, and again
whenever a new copy has been kept since (a redesign draws a new one).
"""
import sqlalchemy as sa
from sqlalchemy import UUID, Column, ForeignKey, Integer, String

from app.core.utils.db_fields import TZAwareDateTime
from app.db.base import AbstractSQLModel


class CertificateFiles(AbstractSQLModel):
    __tablename__ = "certificate_files"

    registration_id = Column(
        UUID(as_uuid=True), ForeignKey("event_registrations_link.id", ondelete="CASCADE"), primary_key=True,
    )
    # The copy's key in the bucket.
    key = Column(String(512), primary_key=True)
    sha256 = Column(String(64), nullable=False)
    size = Column(Integer, nullable=False)
    created_at = Column(TZAwareDateTime(timezone=True), nullable=False, server_default=sa.func.now())
