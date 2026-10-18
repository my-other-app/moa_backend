"""Reading a database error: which constraint, which kind."""
from __future__ import annotations


def _driver_error(exc: BaseException | None):
    """The driver's own exception under SQLAlchemy's wrapping (asyncpg's
    UniqueViolationError and the like), or None."""
    orig = getattr(exc, "orig", None)
    return getattr(orig, "__cause__", None) or orig


def constraint_of(exc: BaseException | None) -> str | None:
    """The name of the constraint or unique index an IntegrityError broke."""
    err = _driver_error(exc)
    name = getattr(err, "constraint_name", None)
    if name:
        return name
    # psycopg2 keeps it on diag.
    diag = getattr(err, "diag", None)
    return getattr(diag, "constraint_name", None)


def sqlstate_of(exc: BaseException | None) -> str | None:
    """The five-character SQLSTATE: 23505 unique, 23503 foreign key, ..."""
    orig = getattr(exc, "orig", None)
    for candidate in (orig, _driver_error(exc)):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if code:
            return str(code)
    return None
