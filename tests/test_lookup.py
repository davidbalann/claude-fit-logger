import httpx
import pytest

import nutrition_lookup as nl


def resp(status=200, payload=None, url="https://x.test/?api_key=SECRET"):
    request = httpx.Request("GET", url)
    if payload is None:
        return httpx.Response(status, request=request, content=b"not json")
    return httpx.Response(status, request=request, json=payload)


def usda_food(fdc_id, description, nutrients, data_type="Foundation"):
    return {"fdcId": fdc_id, "description": description, "dataType": data_type,
            "foodNutrients": nutrients}


def n(name, number, unit, value):
    return {"nutrientName": name, "nutrientNumber": str(number), "unitName": unit, "value": value}


# Lunchmeat, chicken breast: only energy entry is the Atwater one
ATWATER_ONLY = usda_food(1, "Lunchmeat, chicken breast, sliced", [
    n("Protein", 203, "G", 16.0),
    n("Total lipid (fat)", 204, "G", 3.0),
    n("Carbohydrate, by difference", 205, "G", 2.0),
    n("Energy (Atwater General Factors)", 957, "KCAL", 105.0),
])

# kJ listed LAST and also named "Energy": old dict-by-name code stored 695
BOTH_ENERGY = usda_food(4, "Chicken, broilers or fryers, breast, raw", [
    n("Energy", 208, "KCAL", 166.0),
    n("Protein", 203, "G", 22.5),
    n("Total lipid (fat)", 204, "G", 7.8),
    n("Energy", 268, "kJ", 695.0),
])

NO_KCAL = usda_food(9, "Mystery", [n("Energy", 268, "kJ", 400.0), n("Protein", 203, "G", 1.0)])


def test_usda_atwater_only_gives_kcal():
    cand = nl._usda_candidate(ATWATER_ONLY)
    assert cand["per_100g"]["calories"] == 105.0
    assert cand["per_100g"]["protein_g"] == 16.0
    assert cand["id"] == "usda:1" and cand["source"] == "usda"
    assert cand["data_type"] == "Foundation"


def test_usda_prefers_kcal_over_kj_even_when_kj_is_last():
    cand = nl._usda_candidate(BOTH_ENERGY)
    assert cand["per_100g"]["calories"] == 166.0


def test_usda_kj_only_food_is_skipped():
    assert nl._usda_candidate(NO_KCAL) is None


def test_usda_prefers_208_over_atwater():
    food = usda_food(5, "x", [n("Energy (Atwater General Factors)", 957, "KCAL", 100.0),
                              n("Energy", 208, "KCAL", 120.0)])
    assert nl._usda_candidate(food)["per_100g"]["calories"] == 120.0


def test_usda_abridged_detail_format():
    food = {"fdcId": 7, "description": "Apple", "dataType": "SR Legacy", "foodNutrients": [
        {"number": 208, "name": "Energy", "amount": 52, "unitName": "kcal"},
        {"number": 203, "name": "Protein", "amount": 0.3, "unitName": "g"},
    ]}
    cand = nl._usda_candidate(food)
    assert cand["per_100g"]["calories"] == 52.0
    assert cand["per_100g"]["protein_g"] == 0.3


def test_503_retries_once_then_not_found(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return resp(503)

    monkeypatch.setattr(nl.httpx, "get", fake_get)
    assert nl.search_candidates("chicken breast") == []
    # USDA: 1 try + 1 retry, then the fallback search is skipped (source failed).
    # OFF: 1 try + 1 retry.
    assert len(calls) == 4


def test_503_logs_warning_without_key(monkeypatch, caplog):
    monkeypatch.setattr(nl.httpx, "get", lambda url, **kw: resp(503))
    with caplog.at_level("WARNING", logger="claude-fit-logger.lookup"):
        nl.search_candidates("chicken breast")
    text = caplog.text
    assert "USDA lookup failed (attempt 1/2): HTTP 503" in text
    assert "Open Food Facts lookup failed (attempt 2/2): HTTP 503" in text
    assert "SECRET" not in text and "test-usda-key" not in text


def test_retry_succeeds_second_time(monkeypatch):
    seen = {"n": 0}

    def fake_get(url, **kw):
        if url == nl.USDA_SEARCH_URL:
            seen["n"] += 1
            if seen["n"] == 1:
                return resp(503)
            return resp(200, {"foods": [ATWATER_ONLY]})
        return resp(200, {"products": []})

    monkeypatch.setattr(nl.httpx, "get", fake_get)
    cands = nl.search_candidates("chicken breast")
    assert [c["id"] for c in cands] == ["usda:1"]


def test_bad_json_counts_as_failure(monkeypatch):
    monkeypatch.setattr(nl.httpx, "get", lambda url, **kw: resp(200, None))
    assert nl.search_candidates("x") == []


def test_timeout_counts_as_failure(monkeypatch):
    def boom(url, **kw):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(nl.httpx, "get", boom)
    assert nl.search_candidates("x") == []


def test_usda_key_is_sent_as_header_not_in_url(monkeypatch):
    seen = []
    monkeypatch.setattr(nl.httpx, "get", lambda url, **kw: seen.append(kw) or resp(200, {"foods": []}))
    nl._search_usda("apple")
    assert all("api_key" not in (kw.get("params") or {}) for kw in seen)
    assert all(kw["headers"]["X-Api-Key"] == "test-usda-key" for kw in seen)


def off_product(code, name, kcal=None, kj=None):
    nutriments = {"proteins_100g": 5, "carbohydrates_100g": 10, "fat_100g": 2}
    if kcal is not None:
        nutriments["energy-kcal_100g"] = kcal
    if kj is not None:
        nutriments["energy_100g"] = kj
    return {"code": code, "product_name": name, "brands": "Brand", "nutriments": nutriments}


def test_off_skips_missing_kcal_and_never_uses_kj():
    assert nl._off_candidate(off_product("111", "kj only", kj=900), "q") is None
    cand = nl._off_candidate(off_product("222", "ok", kcal=210, kj=900), "q")
    assert cand["per_100g"]["calories"] == 210.0
    assert cand["id"] == "off:222" and cand["source"] == "off"
    assert cand["data_type"] == "Open Food Facts"


def test_candidates_order_and_cap(monkeypatch):
    usda = [usda_food(i, f"u{i}", [n("Energy", 208, "KCAL", 100.0 + i)]) for i in range(1, 6)]
    off = [off_product(str(100 + i), f"o{i}", kcal=50.0 + i) for i in range(5)]

    def fake_get(url, **kw):
        if url == nl.USDA_SEARCH_URL:
            return resp(200, {"foods": usda})
        return resp(200, {"products": off})

    monkeypatch.setattr(nl.httpx, "get", fake_get)
    cands = nl.search_candidates("thing")
    assert len(cands) == 5
    assert [c["source"] for c in cands] == ["usda", "usda", "usda", "off", "off"]


def test_off_tops_up_when_usda_is_empty(monkeypatch):
    off = [off_product(str(100 + i), f"o{i}", kcal=50.0 + i) for i in range(5)]

    def fake_get(url, **kw):
        if url == nl.USDA_SEARCH_URL:
            return resp(200, {"foods": []})
        return resp(200, {"products": off})

    monkeypatch.setattr(nl.httpx, "get", fake_get)
    assert len(nl.search_candidates("thing")) == 5


def test_get_candidate_usda_and_off(monkeypatch):
    def fake_get(url, **kw):
        if url.startswith(nl.USDA_FOOD_URL):
            return resp(200, {"fdcId": 4, "description": "Chicken", "dataType": "SR Legacy",
                              "foodNutrients": [{"number": 208, "name": "Energy", "amount": 166, "unitName": "kcal"}]})
        return resp(200, {"status": 1, "product": off_product("222", "ok", kcal=210)})

    monkeypatch.setattr(nl.httpx, "get", fake_get)
    assert nl.get_candidate("usda:4")["per_100g"]["calories"] == 166.0
    assert nl.get_candidate("off:222")["per_100g"]["calories"] == 210.0


def test_get_candidate_source_down_returns_none(monkeypatch):
    monkeypatch.setattr(nl.httpx, "get", lambda url, **kw: resp(503))
    assert nl.get_candidate("usda:4") is None


@pytest.mark.parametrize("bad", ["", "usda", "usda:abc", "other:12", "12"])
def test_bad_candidate_id_raises(bad):
    with pytest.raises(ValueError):
        nl.parse_candidate_id(bad)


def test_scale_candidate():
    cand = {"per_100g": {"calories": 166.0, "protein_g": 22.5}}
    scaled = nl.scale_candidate(cand, 150)
    assert scaled["calories"] == 249.0
    assert scaled["protein_g"] == 33.8
    assert scaled["fat_g"] == 0.0
