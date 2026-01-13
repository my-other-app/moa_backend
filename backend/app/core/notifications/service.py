"""
Firebase Cloud Messaging notification service.

This module provides functionality to send push notifications to users
via Firebase Cloud Messaging (FCM).
"""

import logging
from typing import Optional
import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.users.models import UserDeviceTokens
from app.config import settings

logger = logging.getLogger(__name__)

# Initialize Firebase Admin SDK
_firebase_app = None


def initialize_firebase():
    """Initialize Firebase Admin SDK with service account credentials."""
    global _firebase_app
    
    if _firebase_app is not None:
        return _firebase_app
    
    try:
        cred = None
        
        # Option 1: Path to service account JSON file
        if settings.FIREBASE_SERVICE_ACCOUNT_PATH:
            logger.info(f"Using Firebase credentials from file: {settings.FIREBASE_SERVICE_ACCOUNT_PATH}")
            cred = credentials.Certificate(settings.FIREBASE_SERVICE_ACCOUNT_PATH)
        
        # Option 2: Base64 encoded service account JSON
        elif settings.FIREBASE_SERVICE_ACCOUNT_BASE64:
            import base64
            import json
            logger.info("Using Firebase credentials from base64 encoded environment variable")
            decoded = base64.b64decode(settings.FIREBASE_SERVICE_ACCOUNT_BASE64)
            service_account_info = json.loads(decoded)
            cred = credentials.Certificate(service_account_info)
        
        if cred:
            _firebase_app = firebase_admin.initialize_app(cred)
            logger.info("Firebase Admin SDK initialized successfully")
        else:
            logger.warning(
                "No Firebase credentials configured. "
                "Set APP_FIREBASE_SERVICE_ACCOUNT_PATH or APP_FIREBASE_SERVICE_ACCOUNT_BASE64"
            )
            return None
        
        return _firebase_app
    except Exception as e:
        logger.error(f"Failed to initialize Firebase Admin SDK: {e}")
        return None


async def get_fcm_tokens_for_users(
    session: AsyncSession, 
    user_ids: list[int]
) -> list[str]:
    """
    Get FCM tokens for a list of users.
    
    Args:
        session: Database session
        user_ids: List of user IDs
        
    Returns:
        List of FCM tokens
    """
    if not user_ids:
        return []
    
    query = select(UserDeviceTokens.fcm_token).where(
        UserDeviceTokens.user_id.in_(user_ids),
        UserDeviceTokens.is_deleted == False,
    )
    result = await session.execute(query)
    tokens = [row[0] for row in result.fetchall()]
    return tokens


async def get_fcm_token_for_user(
    session: AsyncSession,
    user_id: int
) -> Optional[str]:
    """
    Get FCM token for a single user.
    
    Args:
        session: Database session
        user_id: User ID
        
    Returns:
        FCM token or None
    """
    query = select(UserDeviceTokens.fcm_token).where(
        UserDeviceTokens.user_id == user_id,
        UserDeviceTokens.is_deleted == False,
    )
    result = await session.execute(query)
    row = result.first()
    return row[0] if row else None


async def send_notification_to_user(
    session: AsyncSession,
    user_id: int,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """
    Send push notification to a single user.
    
    Args:
        session: Database session
        user_id: Target user ID
        title: Notification title
        body: Notification body
        data: Optional data payload
        
    Returns:
        True if sent successfully, False otherwise
    """
    token = await get_fcm_token_for_user(session, user_id)
    if not token:
        logger.debug(f"No FCM token found for user {user_id}")
        return False
    
    return await _send_to_token(token, title, body, data)


async def send_notification_to_users(
    session: AsyncSession,
    user_ids: list[int],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> int:
    """
    Send push notification to multiple users.
    
    Args:
        session: Database session
        user_ids: List of target user IDs
        title: Notification title
        body: Notification body
        data: Optional data payload
        image_url: Optional image URL to display in notification
        
    Returns:
        Number of successfully sent notifications
    """
    if not user_ids:
        return 0
    
    tokens = await get_fcm_tokens_for_users(session, user_ids)
    if not tokens:
        logger.debug(f"No FCM tokens found for users {user_ids}")
        return 0
    
    return await _send_to_tokens(tokens, title, body, data, image_url)


async def _send_to_token(
    token: str,
    title: str,
    body: str,
    data: Optional[dict] = None,
) -> bool:
    """Send notification to a single FCM token."""
    if not initialize_firebase():
        logger.error("Firebase not initialized, cannot send notification")
        return False
    
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            token=token,
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(
                    sound="default",
                    click_action="FLUTTER_NOTIFICATION_CLICK",
                ),
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=1,
                    ),
                ),
            ),
        )
        
        response = messaging.send(message)
        logger.info(f"Successfully sent notification: {response}")
        return True
        
    except messaging.UnregisteredError:
        logger.warning(f"Token is unregistered, should be removed: {token[:20]}...")
        return False
    except Exception as e:
        logger.error(f"Failed to send notification: {e}")
        return False


async def _send_to_tokens(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    image_url: Optional[str] = None,
) -> int:
    """Send notification to multiple FCM tokens using batch send."""
    if not tokens:
        return 0
    
    if not initialize_firebase():
        logger.error("Firebase not initialized, cannot send notifications")
        return 0
    
    try:
        # Build notification with optional image
        notification = messaging.Notification(
            title=title,
            body=body,
            image=image_url,  # FCM supports image in notification
        )
        
        # Build Android config with optional image
        android_notification = messaging.AndroidNotification(
            sound="default",
            click_action="FLUTTER_NOTIFICATION_CLICK",
            image=image_url,
        )
        
        message = messaging.MulticastMessage(
            notification=notification,
            data=data or {},
            tokens=tokens,
            android=messaging.AndroidConfig(
                priority="high",
                notification=android_notification,
            ),
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound="default",
                        badge=1,
                    ),
                ),
                fcm_options=messaging.APNSFCMOptions(
                    image=image_url,
                ) if image_url else None,
            ),
        )
        
        response = messaging.send_each_for_multicast(message)
        logger.info(
            f"Batch send complete: {response.success_count} success, "
            f"{response.failure_count} failures"
        )
        
        # Log failed tokens for debugging
        if response.failure_count > 0:
            for idx, send_response in enumerate(response.responses):
                if not send_response.success:
                    logger.warning(
                        f"Failed to send to token {idx}: {send_response.exception}"
                    )
        
        return response.success_count
        
    except Exception as e:
        logger.error(f"Failed to send batch notifications: {e}")
        return 0
