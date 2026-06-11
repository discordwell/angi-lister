"""Guards for the naive-UTC datetime convention.

All DateTime columns are `timestamp without time zone`, and both backends
hand values back as naive datetimes (PostgreSQL by definition; the SQLite
dialect drops any UTC offset when parsing stored strings). Writing aware
datetimes therefore creates values that compare unequally with what is read
back, and mixing aware/naive raises TypeError in Python-side comparisons.
The single convention is app.utils.utcnow() — naive UTC — everywhere.
"""

import datetime as dt
import pathlib

from app.models import Base
from app.utils import utcnow

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"


def test_utcnow_is_naive_utc():
    value = utcnow()
    assert value.tzinfo is None
    aware_now = dt.datetime.now(dt.UTC)
    assert abs(aware_now.replace(tzinfo=None) - value) < dt.timedelta(seconds=5)


def test_model_datetime_defaults_are_naive():
    """Every column default/onupdate that produces a datetime must be naive."""
    checked = 0
    for mapper in Base.registry.mappers:
        for col in mapper.columns:
            for hook in (col.default, col.onupdate):
                if hook is None or not getattr(hook, "is_callable", False):
                    continue
                fn = hook.arg
                try:
                    value = fn()
                except TypeError:
                    value = fn(None)
                if isinstance(value, dt.datetime):
                    checked += 1
                    assert value.tzinfo is None, (
                        f"{mapper.class_.__name__}.{col.name} default returns an "
                        "aware datetime; use app.utils.utcnow"
                    )
    assert checked > 0, "expected at least one datetime column default"


def test_no_aware_now_outside_utils():
    """app code must not call dt.datetime.now(dt.UTC) directly."""
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        if path == APP_DIR / "utils.py":
            continue
        if "now(dt.UTC)" in path.read_text():
            offenders.append(str(path.relative_to(APP_DIR.parent)))
    assert not offenders, f"use app.utils.utcnow instead of dt.datetime.now(dt.UTC): {offenders}"
