"""
Nutrition lookup: USDA FoodData Central (better for whole/generic foods) and
Open Food Facts (better for branded/packaged products).

The server never auto-picks a food. `search_candidates` returns up to 5
options (USDA first, then Open Food Facts) and Claude chooses one. Each
candidate has a stateless id ("usda:<fdcId>" or "off:<barcode>") that
`get_candidate` re-fetches on the second log_food call.

Energy is read in kcal only, never kJ. USDA labels energy as "Energy",
"Energy (Atwater General Factors)" or "Energy (Atwater Specific Factors)", and
some foods carry two entries called "Energy" (KCAL and kJ), so entries are
selected by unit and nutrient number, not by name alone. Results with no kcal
value are skipped.

Any lookup failure (HTTP error, timeout, bad JSON) is retried once, logged at
WARNING, then that source counts as "no match".

Required env var:
    USDA_API_KEY   (free, from https://fdc.nal.usda.gov/api-key-signup)
"""

import logging
import os
import time
import httpx

log = logging.getLogger("claude-fit-logger.lookup")

# One retry after a short pause, then the source is treated as "no match".
RETRY_DELAY_S = 1.0

MAX_CANDIDATES = 5
USDA_SHARE = 4  # of the 5 slots, USDA gets up to 4 first; OFF gets up to 1; leftovers top up

USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
USDA_FOOD_URL = "https://api.nal.usda.gov/fdc/v1/food"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
OFF_PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product"

# USDA nutrient numbers, in order of preference for energy:
# 208 = Energy (kcal), 957 = Atwater General Factors, 958 = Atwater Specific Factors.
# 268 is energy in kJ and is never used.
ENERGY_NUMBERS = ("208", "957", "958")

# our key -> (names, numbers). Names are matched exactly, numbers as strings.
USDA_NUTRIENTS = {
    "protein_g": (("Protein",), ("203",)),
    "carbs_g": (("Carbohydrate, by difference",), ("205",)),
    "fat_g": (("Total lipid (fat)",), ("204",)),
    "fiber_g": (("Fiber, total dietary",), ("291",)),
    "sugar_g": (("Sugars, total including NLEA", "Sugars, total"), ("269", "269.3")),
}

NUTRIENT_KEYS = ["calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g"]


def _get_json(source: str, url: str, **kwargs):
    """GET url and return parsed JSON, or None if it failed twice.

    Retries once after RETRY_DELAY_S on HTTP error status, timeout/network
    error, or bad JSON. Each failure is logged at WARNING. Only the source
    name, exception type and status are logged, never the URL or headers
    (the URL can carry an API key)."""
    for attempt in (1, 2):
        try:
            resp = httpx.get(url, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            reason = f"HTTP {e.response.status_code}"
        except httpx.HTTPError as e:  # timeouts, connection errors
            reason = type(e).__name__
        except ValueError:  # bad JSON
            reason = "invalid JSON"
        log.warning("%s lookup failed (attempt %d/2): %s", source, attempt, reason)
        if attempt == 1:
            time.sleep(RETRY_DELAY_S)
    return None


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- USDA ------------------------------------------------------------------

def _normalize_usda_nutrient(n: dict) -> dict:
    """Accepts the search format (nutrientName/nutrientNumber/unitName/value),
    the abridged detail format (name/number/unitName/amount) and the full
    detail format (nutrient{name,number,unitName}/amount)."""
    inner = n.get("nutrient") if isinstance(n.get("nutrient"), dict) else {}
    name = n.get("nutrientName") or n.get("name") or inner.get("name") or ""
    number = n.get("nutrientNumber", n.get("number", inner.get("number", "")))
    unit = n.get("unitName") or inner.get("unitName") or ""
    value = n.get("value", n.get("amount"))
    return {
        "name": str(name),
        "number": str(number).strip(),
        "unit": str(unit).strip().upper(),
        "value": _to_float(value),
    }


def _usda_kcal(nutrients: list):
    """Energy in kcal, or None. Picks by unit and number so a kJ entry that is
    also called "Energy" is never used."""
    best = None
    best_rank = None
    for n in nutrients:
        if n["value"] is None or not n["name"].lower().startswith("energy"):
            continue
        if n["unit"] == "KCAL":
            pass
        elif n["unit"] == "" and n["number"] in ENERGY_NUMBERS:
            pass  # unit missing but the number is a kcal energy number
        else:
            continue  # kJ or anything else
        rank = ENERGY_NUMBERS.index(n["number"]) if n["number"] in ENERGY_NUMBERS else len(ENERGY_NUMBERS)
        if best_rank is None or rank < best_rank:
            best, best_rank = n["value"], rank
    return best


def _usda_per_100g(food: dict):
    """Per-100g nutrients for one USDA food, or None when it has no kcal."""
    nutrients = [_normalize_usda_nutrient(n) for n in food.get("foodNutrients", []) if isinstance(n, dict)]
    kcal = _usda_kcal(nutrients)
    if kcal is None:
        return None
    per_100g = {"calories": kcal}
    for our_key, (names, numbers) in USDA_NUTRIENTS.items():
        for n in nutrients:
            if n["value"] is None or n["unit"] not in ("G", ""):
                continue
            if n["name"] in names or n["number"] in numbers:
                per_100g[our_key] = n["value"]
                break
    return per_100g


def _usda_candidate(food: dict):
    per_100g = _usda_per_100g(food)
    fdc_id = food.get("fdcId")
    if per_100g is None or fdc_id is None:
        return None
    return {
        "id": f"usda:{fdc_id}",
        "name": food.get("description") or "",
        "source": "usda",
        "data_type": food.get("dataType") or "USDA",
        "per_100g": per_100g,
    }


def _usda_headers():
    # The key goes in a header, not the query string, so it never appears in
    # request URLs (httpx logs URLs at INFO, and Render keeps those logs).
    return {"X-Api-Key": os.environ["USDA_API_KEY"]}


def _search_usda(query: str) -> list:
    if not os.environ.get("USDA_API_KEY"):
        return []
    headers = _usda_headers()

    # Prefer generic/whole-food data types over Branded products, since a
    # plain query like "chicken breast" should match the unprocessed food.
    data = _get_json(
        "USDA", USDA_SEARCH_URL,
        params={"query": query, "pageSize": MAX_CANDIDATES,
                "dataType": ["Foundation", "SR Legacy"]},
        headers=headers, timeout=10,
    )
    if data is None:
        return []
    found = [c for c in (_usda_candidate(f) for f in data.get("foods", [])) if c]

    if not found:
        # Fall back to any data type (including Branded) if nothing generic was usable.
        data = _get_json(
            "USDA", USDA_SEARCH_URL,
            params={"query": query, "pageSize": MAX_CANDIDATES},
            headers=headers, timeout=10,
        )
        if data is None:
            return []
        found = [c for c in (_usda_candidate(f) for f in data.get("foods", [])) if c]
    return found


# --- Open Food Facts -------------------------------------------------------

def _off_candidate(product: dict, fallback_name: str):
    n = product.get("nutriments") or {}
    kcal = _to_float(n.get("energy-kcal_100g"))
    code = product.get("code") or product.get("_id")
    if kcal is None or not code:
        return None  # kcal only; never fall back to energy_100g (kJ)
    per_100g = {
        "calories": kcal,
        "protein_g": _to_float(n.get("proteins_100g")),
        "carbs_g": _to_float(n.get("carbohydrates_100g")),
        "fat_g": _to_float(n.get("fat_100g")),
        "fiber_g": _to_float(n.get("fiber_100g")),
        "sugar_g": _to_float(n.get("sugars_100g")),
    }
    per_100g = {k: v for k, v in per_100g.items() if v is not None}
    candidate = {
        "id": f"off:{code}",
        "name": product.get("product_name") or fallback_name,
        "source": "off",
        "data_type": "Open Food Facts",
        "per_100g": per_100g,
    }
    if product.get("brands"):
        candidate["brand"] = product["brands"]
    return candidate


def _search_off(query: str) -> list:
    data = _get_json(
        "Open Food Facts", OFF_SEARCH_URL,
        params={"search_terms": query, "search_simple": 1, "action": "process",
                "json": 1, "page_size": MAX_CANDIDATES},
        timeout=10,
        headers={"User-Agent": "claude-fit-logger/1.0"},
    )
    if data is None:
        return []
    return [c for c in (_off_candidate(p, query) for p in data.get("products", [])) if c]


# --- public API ------------------------------------------------------------

def _safe(source: str, fn, *args):
    """Last-resort guard: an unexpected parsing error in one source counts as
    no match instead of crashing the tool call."""
    try:
        return fn(*args)
    except Exception as e:
        log.warning("%s lookup crashed: %s", source, type(e).__name__)
        return [] if fn in (_search_usda, _search_off) else None


def search_candidates(query: str) -> list:
    """Up to 5 candidates, USDA first then Open Food Facts. Empty list when
    neither source has a usable match (or both are down)."""
    usda = _safe("USDA", _search_usda, query)
    off = _safe("Open Food Facts", _search_off, query)
    chosen = usda[:USDA_SHARE] + off[:MAX_CANDIDATES - USDA_SHARE]
    leftovers = usda[USDA_SHARE:] + off[MAX_CANDIDATES - USDA_SHARE:]
    chosen += leftovers[:MAX_CANDIDATES - len(chosen)]
    # keep USDA entries ahead of OFF entries
    return sorted(chosen, key=lambda c: 0 if c["source"] == "usda" else 1)


def parse_candidate_id(candidate_id: str):
    """Returns (source, raw_id). Raises ValueError for anything malformed."""
    source, sep, raw = str(candidate_id).partition(":")
    raw = raw.strip()
    if sep != ":" or source not in ("usda", "off") or not raw.isdigit():
        raise ValueError(
            "candidate_id must look like 'usda:<number>' or 'off:<barcode>', "
            "copied from a candidates result"
        )
    return source, raw


def _get_usda_food(fdc_id: str):
    if not os.environ.get("USDA_API_KEY"):
        return None
    data = _get_json(
        "USDA", f"{USDA_FOOD_URL}/{fdc_id}",
        params={"format": "abridged"}, headers=_usda_headers(), timeout=10,
    )
    return _usda_candidate(data) if isinstance(data, dict) else None


def _get_off_product(code: str):
    data = _get_json(
        "Open Food Facts", f"{OFF_PRODUCT_URL}/{code}.json",
        params={"fields": "code,product_name,brands,nutriments"},
        headers={"User-Agent": "claude-fit-logger/1.0"}, timeout=10,
    )
    if not isinstance(data, dict) or not isinstance(data.get("product"), dict):
        return None
    product = dict(data["product"])
    product.setdefault("code", code)
    return _off_candidate(product, code)


def get_candidate(candidate_id: str):
    """Re-fetches one candidate by its stateless id. None if the source is
    down or the food has no kcal value. Raises ValueError on a malformed id."""
    source, raw = parse_candidate_id(candidate_id)
    fn = _get_usda_food if source == "usda" else _get_off_product
    try:
        return fn(raw)
    except Exception as e:
        log.warning("%s lookup crashed: %s", source, type(e).__name__)
        return None


def scale_candidate(candidate: dict, quantity_g: float) -> dict:
    """Per-100g values scaled to quantity_g, with every nutrient key present."""
    scale = quantity_g / 100.0
    per_100g = candidate["per_100g"]
    return {k: round(float(per_100g.get(k, 0) or 0) * scale, 1) for k in NUTRIENT_KEYS}
