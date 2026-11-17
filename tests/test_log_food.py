"""log_food wiring in server/main.py, with lookup and Google faked."""

import pytest

import health_client
import nutrition_lookup


@pytest.fixture
def log_food():
    import main
    return main.log_food.fn


CAND = {"id": "usda:4", "name": "Chicken, breast, raw", "source": "usda",
        "data_type": "SR Legacy",
        "per_100g": {"calories": 166.0, "protein_g": 22.5, "fat_g": 7.8}}


@pytest.fixture
def google(monkeypatch):
    calls = []

    def fake_write(nutrients, item_name, tz_name, quantity_g, meal_type=None):
        calls.append(dict(nutrients=nutrients, item_name=item_name,
                          quantity_g=quantity_g, meal_type=meal_type))
        return "users/me/dataTypes/nutrition-log/dataPoints/abc"

    monkeypatch.setattr(health_client, "write_nutrition", fake_write)
    return calls


def test_both_sources_down_returns_not_found_not_an_error(log_food, monkeypatch, google):
    monkeypatch.setattr(nutrition_lookup, "search_candidates", lambda q: [])
    result = log_food("chicken breast", 2, "America/Toronto")
    assert result["status"] == "not_found"
    assert google == []


def test_search_returns_candidates_and_logs_nothing(log_food, monkeypatch, google):
    monkeypatch.setattr(nutrition_lookup, "search_candidates", lambda q: [CAND])
    result = log_food("chicken breast", 100, "America/Toronto")
    assert result["status"] == "candidates"
    assert result["candidates"] == [CAND]
    assert google == []


def test_candidate_id_logs_with_true_source(log_food, monkeypatch, google):
    monkeypatch.setattr(nutrition_lookup, "get_candidate", lambda cid: CAND)
    result = log_food("chicken breast", 150, "America/Toronto", candidate_id="usda:4")
    assert result["status"] == "logged"
    assert result["source"] == "usda" and result["estimated"] is False
    assert result["nutrients"]["calories"] == 249.0
    assert google[0]["quantity_g"] == 150


def test_candidate_fetch_failure_is_not_found(log_food, monkeypatch, google):
    monkeypatch.setattr(nutrition_lookup, "get_candidate", lambda cid: None)
    result = log_food("chicken breast", 150, "America/Toronto", candidate_id="usda:4")
    assert result["status"] == "not_found"
    assert google == []


def test_manual_values_logged_as_estimate_with_meal_type(log_food, google):
    result = log_food("chicken breast", 100, "America/Toronto", calories=165,
                      protein_g=31, fat_g=3.6, meal_type="LUNCH")
    assert result["status"] == "logged"
    assert result["source"] == "claude_estimate" and result["estimated"] is True
    assert google[0]["meal_type"] == "LUNCH"
    assert result["today_totals"]["entry_count"] == 1


def test_bad_quantity_raises(log_food):
    with pytest.raises(ValueError):
        log_food("x", 0, "America/Toronto", calories=1)
