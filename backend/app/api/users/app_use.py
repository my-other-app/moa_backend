"""Whether a guest uses the MyOtherApp app: some phone is signed in to it.

The app registers a push token for the account when someone signs in on a
phone (user_device_tokens), and the token goes when they sign out there. So a
live token is "this person has the app on a phone, signed in". Someone who
installed it and never signed in, or signed out, does not count."""
from sqlalchemy import exists

from app.api.users.models import UserDeviceTokens


def on_app(user_id_column):
    """A WHERE condition: the account in `user_id_column` is signed in to the app on some phone."""
    return exists().where(UserDeviceTokens.user_id == user_id_column, UserDeviceTokens.is_deleted == False)  # noqa: E712
