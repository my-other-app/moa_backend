"""
Apple Wallet Pass Generation Service

Generates .pkpass files for event tickets that can be added to Apple Wallet.

Prerequisites:
1. Apple Developer account with Pass Type ID configured
2. Pass certificate files in /wallet_certs/ directory:
   - pass_certificate.pem
   - pass_key.pem  
   - wwdr.pem (Apple WWDR certificate)

Environment variables:
   - WALLET_PASS_TYPE_ID: e.g., "pass.com.myotherapp.ticket"
   - WALLET_TEAM_ID: Apple Developer Team ID
   - WALLET_ORGANIZATION_NAME: e.g., "My Other App"
"""

import os
import io
import hashlib
import json
import logging
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.response import CustomHTTPException
from app.api.events.models import Events, EventRegistrationsLink

logger = logging.getLogger(__name__)


# Configuration from environment
PASS_TYPE_ID = os.getenv("WALLET_PASS_TYPE_ID", "pass.com.myotherapp.ticket")
TEAM_ID = os.getenv("WALLET_TEAM_ID", "")
ORG_NAME = os.getenv("WALLET_ORGANIZATION_NAME", "My Other App")

# Certificate paths
CERTS_DIR = Path(__file__).parent.parent.parent.parent / "wallet_certs"
CERT_PATH = CERTS_DIR / "pass_certificate.pem"
KEY_PATH = CERTS_DIR / "pass_key.pem"
WWDR_PATH = CERTS_DIR / "wwdr.pem"


async def get_registration_by_ticket_id(
    session: AsyncSession, 
    ticket_id: str
) -> tuple[EventRegistrationsLink, Events]:
    """Get registration and event data by ticket ID."""
    query = (
        select(EventRegistrationsLink)
        .where(EventRegistrationsLink.ticket_id == ticket_id)
        .where(EventRegistrationsLink.is_deleted == False)
        .options(selectinload(EventRegistrationsLink.event))
    )
    
    result = await session.execute(query)
    registration = result.scalar_one_or_none()
    
    if not registration:
        raise CustomHTTPException(404, message="Ticket not found")
    
    if not registration.event:
        raise CustomHTTPException(404, message="Event not found")
        
    return registration, registration.event


def generate_pass_json(
    ticket_id: str,
    event_name: str,
    event_datetime: datetime,
    location_name: Optional[str],
    holder_name: str,
    organization_name: str = ORG_NAME,
) -> dict:
    """Generate the pass.json structure for Apple Wallet."""
    
    # Format date/time
    date_str = event_datetime.strftime("%b %d, %Y") if event_datetime else "TBA"
    time_str = event_datetime.strftime("%I:%M %p") if event_datetime else "TBA"
    iso_date = event_datetime.isoformat() if event_datetime else None
    
    pass_json = {
        "formatVersion": 1,
        "passTypeIdentifier": PASS_TYPE_ID,
        "serialNumber": ticket_id,
        "teamIdentifier": TEAM_ID,
        "organizationName": organization_name,
        "description": f"Ticket for {event_name}",
        "logoText": organization_name,
        "foregroundColor": "rgb(255, 255, 255)",
        "backgroundColor": "rgb(26, 26, 46)",  # Dark theme matching app
        "labelColor": "rgb(249, 255, 161)",  # Accent yellow
        
        # Barcode
        "barcodes": [
            {
                "message": ticket_id,
                "format": "PKBarcodeFormatQR",
                "messageEncoding": "iso-8859-1"
            }
        ],
        
        # Event ticket style
        "eventTicket": {
            "primaryFields": [
                {
                    "key": "event",
                    "label": "EVENT",
                    "value": event_name
                }
            ],
            "secondaryFields": [
                {
                    "key": "date",
                    "label": "DATE",
                    "value": date_str
                },
                {
                    "key": "time", 
                    "label": "TIME",
                    "value": time_str
                }
            ],
            "auxiliaryFields": [
                {
                    "key": "location",
                    "label": "LOCATION",
                    "value": location_name or "TBA"
                },
                {
                    "key": "holder",
                    "label": "ATTENDEE",
                    "value": holder_name
                }
            ],
            "backFields": [
                {
                    "key": "ticketId",
                    "label": "Ticket ID",
                    "value": ticket_id
                },
                {
                    "key": "terms",
                    "label": "Terms & Conditions",
                    "value": "This ticket is non-transferable. Present this pass at the venue for entry."
                }
            ]
        }
    }
    
    # Add relevant date if available
    if iso_date:
        pass_json["relevantDate"] = iso_date
    
    return pass_json


def create_manifest(files: dict[str, bytes]) -> bytes:
    """Create manifest.json with SHA1 hashes of all files."""
    manifest = {}
    for filename, content in files.items():
        sha1_hash = hashlib.sha1(content).hexdigest()
        manifest[filename] = sha1_hash
    return json.dumps(manifest, indent=2).encode('utf-8')


def sign_manifest(manifest_bytes: bytes) -> bytes:
    """
    Sign the manifest using OpenSSL.
    Returns the PKCS#7 signature.
    
    NOTE: This requires the certificate files to be present.
    If certificates are not configured, returns empty bytes (pass won't work on device).
    """
    import subprocess
    import tempfile
    
    # Debug logging for certificate paths
    logger.info(f"Checking certificates in: {CERTS_DIR}")
    logger.info(f"CERT_PATH exists: {CERT_PATH.exists()} - {CERT_PATH}")
    logger.info(f"KEY_PATH exists: {KEY_PATH.exists()} - {KEY_PATH}")
    logger.info(f"WWDR_PATH exists: {WWDR_PATH.exists()} - {WWDR_PATH}")
    
    # Check if certificates exist
    if not all(p.exists() for p in [CERT_PATH, KEY_PATH, WWDR_PATH]):
        # Return empty signature if certs don't exist
        logger.error("One or more certificate files are missing!")
        raise CustomHTTPException(500, "Wallet certificates are not configured on the server.")
    
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.json') as manifest_file:
            manifest_file.write(manifest_bytes)
            manifest_path = manifest_file.name
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.sig') as sig_file:
            sig_path = sig_file.name
        
        # Sign using OpenSSL PKCS#7
        cmd = [
            "openssl", "smime", "-sign",
            "-signer", str(CERT_PATH),
            "-inkey", str(KEY_PATH),
            "-certfile", str(WWDR_PATH),
            "-in", manifest_path,
            "-out", sig_path,
            "-outform", "DER",
            "-binary"
        ]
        
        logger.info(f"Running OpenSSL command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True)
        
        if result.returncode != 0:
            logger.error(f"OpenSSL signing failed with code {result.returncode}")
            logger.error(f"OpenSSL stderr: {result.stderr.decode()}")
            logger.error(f"OpenSSL stdout: {result.stdout.decode()}")
            logger.error(f"OpenSSL stderr: {result.stderr.decode()}")
            logger.error(f"OpenSSL stdout: {result.stdout.decode()}")
            raise CustomHTTPException(500, "Failed to sign wallet pass.")
        
        with open(sig_path, 'rb') as f:
            signature = f.read()
        
        logger.info(f"Signature generated successfully, size: {len(signature)} bytes")
        
        # Cleanup
        os.unlink(manifest_path)
        os.unlink(sig_path)
        
        return signature
        
    except Exception as e:
        logger.exception(f"Error signing manifest: {e}")
    except Exception as e:
        logger.exception(f"Error signing manifest: {e}")
        raise CustomHTTPException(500, f"Error signing wallet pass: {str(e)}")


def get_default_icon() -> bytes:
    """Generate a simple default icon (1x1 green pixel PNG)."""
    # Minimal valid PNG - 1x1 pixel, lime/yellow color
    # In production, replace with actual logo assets
    import struct
    import zlib
    
    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        chunk_len = struct.pack('>I', len(data))
        chunk_crc = struct.pack('>I', zlib.crc32(chunk_type + data) & 0xffffffff)
        return chunk_len + chunk_type + data + chunk_crc
    
    # PNG header
    png_header = b'\x89PNG\r\n\x1a\n'
    
    # IHDR chunk: 1x1, 8-bit RGB
    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    ihdr = png_chunk(b'IHDR', ihdr_data)
    
    # IDAT chunk: single lime/yellow pixel (F9, FF, A1)
    raw_data = b'\x00\xf9\xff\xa1'  # filter byte + RGB
    compressed = zlib.compress(raw_data)
    idat = png_chunk(b'IDAT', compressed)
    
    # IEND chunk
    iend = png_chunk(b'IEND', b'')
    
    return png_header + ihdr + idat + iend


def create_pkpass(pass_json: dict) -> bytes:
    """
    Create a .pkpass file (signed ZIP archive).
    
    Returns the binary content of the .pkpass file.
    """
    # Collect all files for the pass
    files = {}
    
    # pass.json
    pass_json_bytes = json.dumps(pass_json, indent=2).encode('utf-8')
    files['pass.json'] = pass_json_bytes
    
    # Add icon images (required)
    # In production, use actual logo/icon assets
    icon_bytes = get_default_icon()
    files['icon.png'] = icon_bytes
    files['icon@2x.png'] = icon_bytes
    files['icon@3x.png'] = icon_bytes
    files['logo.png'] = icon_bytes
    files['logo@2x.png'] = icon_bytes
    
    # Create and sign manifest
    manifest_bytes = create_manifest(files)
    files['manifest.json'] = manifest_bytes
    
    signature = sign_manifest(manifest_bytes)
    if signature:
        files['signature'] = signature
    
    # Create ZIP archive
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename, content in files.items():
            zf.writestr(filename, content)
    
    return buffer.getvalue()


async def generate_wallet_pass(
    session: AsyncSession,
    ticket_id: str
) -> bytes:
    """
    Generate an Apple Wallet .pkpass file for the given ticket.
    
    Args:
        session: Database session
        ticket_id: The ticket ID to generate pass for
        
    Returns:
        Binary content of the .pkpass file
    """
    # Get registration and event data
    registration, event = await get_registration_by_ticket_id(session, ticket_id)
    
    # Generate pass JSON
    pass_json = generate_pass_json(
        ticket_id=registration.ticket_id,
        event_name=event.name,
        event_datetime=event.event_datetime,
        location_name=event.location_name,
        holder_name=registration.full_name,
    )
    
    # Create the .pkpass file
    pkpass_bytes = create_pkpass(pass_json)
    
    return pkpass_bytes
