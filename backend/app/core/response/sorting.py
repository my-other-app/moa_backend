"""Shared ordering for the admin list endpoints.

Every admin list had a hard-coded `order_by` and no way to change it, so the
dashboard could search but never sort. This centralises the part that must not
be got wrong: the sort key is matched against a whitelist of columns the
endpoint declares, never interpolated, so an unexpected value falls back to
the default instead of reaching the query.
"""

from typing import Any, Mapping

from sqlalchemy import asc, desc

SortColumns = Mapping[str, Any]


def apply_sort(
    query,
    columns: SortColumns,
    default: Any,
    sort: str | None = None,
    order: str | None = None,
    default_order: str = "desc",
):
    """
    Order `query` by a named column.

    `columns` maps the names the API accepts to the columns they sort on.
    Anything not in it -- including None -- uses `default`, so a stale
    bookmark or a typo returns a sensible page rather than a 422 or an error.

    A secondary sort on the default column keeps paging stable: without it,
    rows sharing a value (every club with no events, say) can appear on two
    pages or neither as the database is free to return ties in any order.
    """
    column = columns.get(sort) if sort else None
    if column is None:
        column = default

    direction = desc if (order or default_order).lower() != "asc" else asc
    ordered = query.order_by(direction(column))
    return ordered if column is default else ordered.order_by(desc(default))
