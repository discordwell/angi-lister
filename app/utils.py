"""Shared helpers."""

import datetime as dt


def utcnow() -> dt.datetime:
    """Current UTC time as a naive datetime.

    All DateTime columns are `timestamp without time zone`, so naive UTC is
    the single convention for values written to and read from the database.
    An aware datetime would round-trip aware on SQLite but naive on
    PostgreSQL, so mixing the two makes comparisons fail on exactly one
    backend.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)
