"""Passwords are hashed at bcrypt cost 12 in production. The suite hashes at
the lowest cost (tests/conftest.py, _cheap_password_hashes) so it runs in
minutes rather than an hour; these keep that a test-only setting."""
import bcrypt

from app.core.auth.authentication import DUMMY_HASH, get_password_hash, verify_password
from tests.conftest import TEST_BCRYPT_ROUNDS


def test_production_hashes_passwords_at_cost_12(monkeypatch, _cheap_password_hashes):
    # The code that runs in production, with bcrypt's own salt maker back.
    monkeypatch.setattr(bcrypt, "gensalt", _cheap_password_hashes)
    hashed = get_password_hash("Password123!")
    assert hashed.startswith("$2b$12$")
    assert verify_password("Password123!", hashed)
    assert not verify_password("password123!", hashed)


def test_a_sign_in_with_no_account_is_checked_at_the_same_cost():
    # A refusal for an address with no account takes as long as one for a
    # wrong password only while the dummy costs what a real hash does.
    assert DUMMY_HASH.startswith("$2b$12$")
    assert not verify_password("Password123!", DUMMY_HASH)


def test_the_suite_hashes_at_the_lowest_cost():
    hashed = get_password_hash("Password123!")
    assert hashed.startswith(f"$2b${TEST_BCRYPT_ROUNDS:02d}$")
    # Still a real bcrypt hash: the sign-in paths read it as one.
    assert verify_password("Password123!", hashed)
    assert not verify_password("Password123?", hashed)
