"""What a club is told when AI assist cannot do what it asked.

A contract shared with the dashboard:

- 403 AI_DISABLED: the server's switch (APP_AI_ENABLED) is off.
- 403 PLAN_FEATURE_LOCKED: the club's plan has no AI assist (the plan
  gates' own shape, subscriptions.gates.feature_locked).
- 429 AI_QUOTA_REACHED: this month's actions are used; the message says how
  many of how many, and `errors` carries the numbers, when they come back,
  and the cheapest plan with more.
- 503 AI_UNAVAILABLE: no model gave a usable answer (or nothing is set up).
- On the club's own key, never the platform's in its place:
  401 AI_KEY_INVALID (refused or revoked) and 402 AI_KEY_QUOTA (out of
  credit, quota or rate limit). Saving a key that fails its test call is
  422 AI_KEY_INVALID.
"""
from __future__ import annotations

from app.core.ai import runner
from app.core.ai.base import AUTH, BILLING, RATE_LIMITED
from app.response import CustomHTTPException

AI_DISABLED = "AI_DISABLED"
AI_UNAVAILABLE = "AI_UNAVAILABLE"
AI_QUOTA_REACHED = "AI_QUOTA_REACHED"
AI_KEY_INVALID = "AI_KEY_INVALID"
AI_KEY_QUOTA = "AI_KEY_QUOTA"

#: How each provider is named to a club.
PROVIDER_NAMES = {"gemini": "Google Gemini", "anthropic": "Anthropic"}


def provider_name(provider: str) -> str:
    return PROVIDER_NAMES.get(provider, provider or "the provider")


def disabled() -> CustomHTTPException:
    return CustomHTTPException(status_code=403, message="AI assist isn't turned on yet.", error_code=AI_DISABLED)


def unavailable(message: str = "AI assist couldn't get an answer right now. Try again in a minute.") -> CustomHTTPException:
    return CustomHTTPException(status_code=503, message=message, error_code=AI_UNAVAILABLE)


def key_invalid(message: str, *, status: int = 401) -> CustomHTTPException:
    return CustomHTTPException(status_code=status, message=message, error_code=AI_KEY_INVALID)


def key_quota(provider: str) -> CustomHTTPException:
    return CustomHTTPException(
        status_code=402,
        message=(
            f"Your club's {provider_name(provider)} key is out of credit or quota, or over its rate limit. "
            f"Check its plan with {provider_name(provider)}, or try again in a minute."
        ),
        error_code=AI_KEY_QUOTA,
    )


def from_failure(failure: runner.AIFailure, *, provider: str) -> CustomHTTPException:
    """The answer for a failed action. A club's own key is never quietly
    swapped for the platform's: what is wrong with it is said, so it can be
    fixed."""
    if failure.byok:
        if failure.kind == AUTH:
            return key_invalid(
                f"{provider_name(provider)} refused your club's AI key. It may have been revoked or changed: "
                "connect it again in Settings."
            )
        if failure.kind in (BILLING, RATE_LIMITED):
            return key_quota(provider)
    if failure.kind == runner.INVALID_OUTPUT:
        return unavailable("The AI's answer couldn't be used. Try again.")
    return unavailable()
