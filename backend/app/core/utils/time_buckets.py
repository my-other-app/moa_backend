"""Timezone-stable date bucketing for analytics queries."""

from sqlalchemy import Date, cast, func, literal_column

# Rendered inline rather than passed as a bind parameter. Postgres compares
# GROUP BY expressions textually, before binding, so `timezone($1, col)` in the
# SELECT and `timezone($4, col)` in the GROUP BY are not recognised as the same
# expression and the query fails with a GroupingError.
_UTC = literal_column("'UTC'")


def utc_ts(column):
    """`column` as a naive timestamp in UTC.

    Every date bucket in analytics goes through this. `date_trunc` and
    `cast(... AS date)` on a `timestamptz` resolve using the *database
    session's* TimeZone, but the day keys the results are looked up by are
    built from `datetime.now(timezone.utc)` in Python. On a server set to
    anything other than UTC the two disagree for part of every day -- on
    Asia/Kolkata, between 18:30 and 00:00 UTC -- and every row created in that
    window buckets to a day the series never asks for, so it reads as zero.

    Production happens to run Postgres in UTC, which is why this stayed
    invisible; it is one `SET TimeZone` away from silently shifting every chart
    on the platform by a day for a quarter of each day.
    """
    return func.timezone(_UTC, column)


def utc_date(column):
    """`column` as a UTC calendar date."""
    return cast(utc_ts(column), Date)


def utc_trunc(precision: str, column):
    """`date_trunc(precision, column)` evaluated in UTC."""
    return func.date_trunc(precision, utc_ts(column))
