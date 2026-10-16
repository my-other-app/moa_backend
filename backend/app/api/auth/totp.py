"""Time-based one-time passwords for admin accounts.

A stolen admin password was the whole chain: impersonation, refunds and
deletion all sit behind it, and nothing else stood in the way.

Enrolment is two steps on purpose -- a secret is generated and only enabled
once the user proves they can read a code from it. Enabling on generation
would lock out anyone whose authenticator failed to scan.
"""

import secrets

import pyotp

from app.core.auth.authentication import get_password_hash, verify_password

ISSUER = "MyOtherApp Admin"
RECOVERY_CODE_COUNT = 8


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account: str) -> str:
    """The otpauth:// URI an authenticator app scans."""
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=ISSUER)


def verify(secret: str, code: str) -> bool:
    """
    A one-step window either side, which is the usual allowance for clock
    drift between a phone and a server. Wider would meaningfully extend how
    long a shoulder-surfed code stays valid.
    """
    if not secret or not code:
        return False
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """
    Returns (plaintext, hashed).

    The plaintext is shown once and never stored; the hashes are what the
    database keeps, because a recovery code is a password equivalent and a
    leaked table should not hand someone a way past 2FA.
    """
    plaintext = [
        f"{secrets.token_hex(2)}-{secrets.token_hex(2)}"
        for _ in range(RECOVERY_CODE_COUNT)
    ]
    return plaintext, [get_password_hash(code) for code in plaintext]


def consume_recovery_code(code: str, hashes: list[str] | None):
    """
    Returns the remaining hashes if the code matched, else None.

    A used code is removed rather than left usable: a recovery code is a
    single-use bypass, and leaving it active turns one leaked code into
    permanent access.
    """
    if not hashes:
        return None
    candidate = code.strip().replace(" ", "")
    for stored in hashes:
        if verify_password(candidate, stored):
            return [h for h in hashes if h != stored]
    return None
