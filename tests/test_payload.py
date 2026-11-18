import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import health_client as hc

UNIT = "users/me/dataTypes/food-measurement-unit/dataPoints/gram-id"
NOW = datetime(2026, 11, 16, 23, 31, 56, tzinfo=ZoneInfo("America/Toronto"))
NUTRIENTS = {"calories": 165, "protein_g": 31, "carbs_g": 0, "fat_g": 3.6,
             "fiber_g": 0, "sugar_g": 0}


def build(**overrides):
    args = dict(nutrients=NUTRIENTS, item_name="chicken breast", quantity_g=100,
                now_local=NOW, meal_type=None, unit_name=UNIT)
    args.update(overrides)
    return hc.build_nutrition_payload(**args)["nutritionLog"]


def test_serving_has_unit_and_amount():
    serving = build()["serving"]
    assert serving == {"amount": 100.0, "foodMeasurementUnit": UNIT}


def test_core_fields():
    log = build()
    assert log["foodDisplayName"] == "chicken breast"
    assert log["energy"] == {"kcal": 165.0}
    assert log["totalCarbohydrate"] == {"grams": 0.0}
    assert log["totalFat"] == {"grams": 3.6}
    assert {"nutrient": "PROTEIN", "quantity": {"grams": 31.0}} in log["nutrients"]
    assert {"nutrient": "DIETARY_FIBER", "quantity": {"grams": 0.0}} in log["nutrients"]
    assert {"nutrient": "SUGAR", "quantity": {"grams": 0.0}} in log["nutrients"]


def test_interval_uses_local_offset_and_one_minute():
    interval = build()["interval"]
    assert interval["startUtcOffset"] == "-18000s"  # EST in November
    assert interval["endUtcOffset"] == "-18000s"
    assert interval["startTime"] == "2026-11-17T04:31:56Z"
    assert interval["endTime"] == "2026-11-17T04:32:56Z"


def test_meal_type_inferred_and_never_unspecified():
    assert build()["mealType"] == "SNACK"  # 23:31 local
    for hour in range(24):
        now = NOW.replace(hour=hour)
        meal = build(now_local=now)["mealType"]
        assert meal in hc.MEAL_TYPES
        assert "UNSPECIFIED" not in json.dumps(build(now_local=now))


def test_explicit_meal_type_used():
    assert build(meal_type="breakfast")["mealType"] == "BREAKFAST"


def test_unspecified_meal_type_is_rejected():
    with pytest.raises(ValueError):
        build(meal_type="MEAL_TYPE_UNSPECIFIED")


def test_missing_unit_is_rejected():
    with pytest.raises(ValueError):
        build(unit_name="")


@pytest.mark.parametrize("qty", [0, -5, None])
def test_bad_quantity_is_rejected(qty):
    with pytest.raises(ValueError):
        build(quantity_g=qty)


def test_missing_optional_nutrients_are_omitted():
    log = build(nutrients={"calories": 10})
    assert "nutrients" not in log
    assert log["totalFat"] == {"grams": 0.0}


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def test_write_nutrition_sends_built_payload_and_unit(monkeypatch):
    hc._unit_cache.clear()
    sent = {}
    monkeypatch.setattr(hc, "_headers", lambda: {"Authorization": "Bearer x"})
    monkeypatch.setattr(hc.httpx, "get", lambda url, **kw: FakeResponse(200, {"dataPoints": [
        {"name": "users/me/dataTypes/food-measurement-unit/dataPoints/cup-id", "displayName": "cup"},
        {"name": UNIT, "displayName": "gram"},
    ]}))

    def fake_post(url, **kw):
        sent["url"], sent["json"] = url, kw["json"]
        return FakeResponse(200, {"response": {"name": "users/me/dataTypes/nutrition-log/dataPoints/abc"}})

    monkeypatch.setattr(hc.httpx, "post", fake_post)
    name = hc.write_nutrition(NUTRIENTS, "chicken breast", "America/Toronto", quantity_g=100)
    assert name.endswith("/abc")
    serving = sent["json"]["nutritionLog"]["serving"]
    assert serving == {"amount": 100.0, "foodMeasurementUnit": UNIT}
    assert sent["json"]["nutritionLog"]["mealType"] in hc.MEAL_TYPES


def test_google_400_raises_with_google_body(monkeypatch):
    hc._unit_cache.clear()
    hc._unit_cache["gram"] = UNIT
    monkeypatch.setattr(hc, "_headers", lambda: {"Authorization": "Bearer x"})
    body = '{"error": {"message": "Required field \'serving.food_measurement_unit\' is missing."}}'
    monkeypatch.setattr(hc.httpx, "post", lambda url, **kw: FakeResponse(400, text=body))
    with pytest.raises(hc.GoogleHealthError) as exc:
        hc.write_nutrition(NUTRIENTS, "x", "America/Toronto", quantity_g=1)
    assert "food_measurement_unit" in str(exc.value)
    assert not hc._unit_cache  # stale unit cache is dropped on a 400


def test_find_gram_unit_handles_shapes():
    assert hc._find_gram_unit([{"name": UNIT, "foodMeasurementUnit": {"displayName": "Gram"}}]) == UNIT
    assert hc._find_gram_unit([{"name": "users/me/dataTypes/food-measurement-unit/dataPoints/g"}]) != ""
    assert hc._find_gram_unit([{"name": "x/kilogram-id", "displayName": "kilogram"}]) == ""
    assert hc._find_gram_unit([]) == ""


def test_missing_gram_unit_raises_and_shows_list(monkeypatch):
    hc._unit_cache.clear()
    monkeypatch.setattr(hc, "_headers", lambda: {})
    monkeypatch.setattr(hc.httpx, "get", lambda url, **kw: FakeResponse(
        200, {"dataPoints": [{"name": "u/cup-id", "displayName": "cup"}]}))
    with pytest.raises(hc.GoogleHealthError) as exc:
        hc._resolve_gram_unit()
    assert "cup" in str(exc.value)


UNIT_OK = {"dataPoints": [{"name": UNIT, "displayName": "gram"}]}
UNAVAILABLE = '{"error": {"code": 503, "message": "The service is currently unavailable.", "status": "UNAVAILABLE"}}'
CREATED = {"response": {"name": "users/me/dataTypes/nutrition-log/dataPoints/abc"}}


def test_unit_lookup_stops_paging_at_first_match(monkeypatch):
    hc._unit_cache.clear()
    monkeypatch.setattr(hc, "_headers", lambda: {})
    calls = []

    def fake_get(url, **kw):
        calls.append(kw.get("params"))
        if len(calls) == 1:
            return FakeResponse(200, {"dataPoints": [{"name": "u/cup", "displayName": "cup"}],
                                      "nextPageToken": "p2"})
        return FakeResponse(200, {"dataPoints": [{"name": UNIT, "displayName": "gram"}],
                                  "nextPageToken": "p3"})

    monkeypatch.setattr(hc.httpx, "get", fake_get)
    assert hc._resolve_gram_unit() == UNIT
    assert len(calls) == 2  # never asked for page 3


def test_unit_lookup_retries_503_then_succeeds(monkeypatch):
    hc._unit_cache.clear()
    monkeypatch.setattr(hc, "_headers", lambda: {})
    responses = [FakeResponse(503, text=UNAVAILABLE), FakeResponse(503, text=UNAVAILABLE),
                 FakeResponse(200, UNIT_OK)]
    monkeypatch.setattr(hc.httpx, "get", lambda url, **kw: responses.pop(0))
    assert hc._resolve_gram_unit() == UNIT
    assert not responses


def test_unit_lookup_gives_up_after_three_503s(monkeypatch):
    hc._unit_cache.clear()
    monkeypatch.setattr(hc, "_headers", lambda: {})
    calls = []
    monkeypatch.setattr(hc.httpx, "get",
                        lambda url, **kw: calls.append(1) or FakeResponse(503, text=UNAVAILABLE))
    with pytest.raises(hc.GoogleHealthError) as exc:
        hc._resolve_gram_unit()
    assert len(calls) == 3
    assert "UNAVAILABLE" in str(exc.value)


def test_write_retries_503_then_succeeds(monkeypatch):
    hc._unit_cache["gram"] = UNIT
    monkeypatch.setattr(hc, "_headers", lambda: {})
    responses = [FakeResponse(503, text=UNAVAILABLE), FakeResponse(503, text=UNAVAILABLE),
                 FakeResponse(200, CREATED)]
    monkeypatch.setattr(hc.httpx, "post", lambda url, **kw: responses.pop(0))
    assert hc.write_nutrition(NUTRIENTS, "x", "America/Toronto", quantity_g=1).endswith("/abc")
    assert not responses


def test_write_gives_up_after_three_503s(monkeypatch):
    hc._unit_cache["gram"] = UNIT
    monkeypatch.setattr(hc, "_headers", lambda: {})
    calls = []
    monkeypatch.setattr(hc.httpx, "post",
                        lambda url, **kw: calls.append(1) or FakeResponse(503, text=UNAVAILABLE))
    with pytest.raises(hc.GoogleHealthError) as exc:
        hc.write_nutrition(NUTRIENTS, "x", "America/Toronto", quantity_g=1)
    assert len(calls) == 3
    assert "UNAVAILABLE" in str(exc.value)


@pytest.mark.parametrize("status", [400, 401, 403, 500])
def test_write_does_not_retry_other_errors(monkeypatch, status):
    hc._unit_cache["gram"] = UNIT
    monkeypatch.setattr(hc, "_headers", lambda: {})
    calls = []
    monkeypatch.setattr(hc.httpx, "post",
                        lambda url, **kw: calls.append(1) or FakeResponse(status, text="{}"))
    with pytest.raises(hc.GoogleHealthError):
        hc.write_nutrition(NUTRIENTS, "x", "America/Toronto", quantity_g=1)
    assert len(calls) == 1
