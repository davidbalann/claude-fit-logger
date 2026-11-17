from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import health_client as hc

TZ = ZoneInfo("America/Toronto")


@pytest.mark.parametrize("hour,expected", [
    (0, "SNACK"), (4, "SNACK"),
    (5, "BREAKFAST"), (10, "BREAKFAST"),
    (11, "LUNCH"), (14, "LUNCH"),
    (15, "SNACK"), (16, "SNACK"),
    (17, "DINNER"), (20, "DINNER"),
    (21, "SNACK"), (23, "SNACK"),
])
def test_infer_meal_type_by_hour(hour, expected):
    assert hc.infer_meal_type(hour) == expected


def test_every_hour_gives_a_valid_meal_type():
    for hour in range(24):
        assert hc.infer_meal_type(hour) in hc.MEAL_TYPES


def test_resolve_uses_local_hour_when_not_given():
    lunch = datetime(2026, 11, 17, 12, 30, tzinfo=TZ)
    assert hc.resolve_meal_type(None, lunch) == "LUNCH"
    assert hc.resolve_meal_type("", lunch) == "LUNCH"


def test_resolve_accepts_explicit_value_case_insensitive():
    now = datetime(2026, 11, 17, 12, 30, tzinfo=TZ)
    assert hc.resolve_meal_type("dinner", now) == "DINNER"
    assert hc.resolve_meal_type(" Snack ", now) == "SNACK"


@pytest.mark.parametrize("bad", ["MEAL_TYPE_UNSPECIFIED", "brunch", "UNSPECIFIED"])
def test_resolve_rejects_unspecified_and_unknown(bad):
    now = datetime(2026, 11, 17, 12, 30, tzinfo=TZ)
    with pytest.raises(ValueError):
        hc.resolve_meal_type(bad, now)
