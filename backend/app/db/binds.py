"""A list of values bound as one parameter."""
from __future__ import annotations

from typing import Iterable

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY


def any_of(column, values: Iterable):
    """`column IN (values)`, written `column = ANY(:values)` with the list
    bound as one array of the column's type.

    An IN list binds one parameter per value, and the driver (asyncpg)
    refuses a statement with more than 32,767 of them, so a list built from
    a whole event's or club's people stops working past that many. One
    array is one parameter however long it is, and the planner still uses
    the column's index for it. An empty list matches nothing, as IN () does."""
    return column == sa.any_(sa.literal(list(values), ARRAY(column.type)))
