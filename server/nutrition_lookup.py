"""
Nutrition lookup: tries USDA FoodData Central first (better for whole/generic
foods), then Open Food Facts (better for branded/packaged products).
Returns None if neither has a match — the caller (main.py) treats that as
a signal to ask Claude to estimate instead.

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

USDA_SEARCH_URL = "https://api.nal.usda.gov/fdc/v1/foods/search"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"

# Normalized nutrient key -> possible USDA nutrient names (USDA is inconsistent
# across data types, so we match on a few known aliases per nutrient)
USDA_NUTRIENT_ALIASES = {
    "calories": ["Energy"],
    "protein_g": ["Protein"],
    "carbs_g": ["Carbohydrate, by difference"],
    "fat_g": ["Total lipid (fat)"],
    "fiber_g": ["Fiber, total dietary"],
    "sugar_g": ["Sugars, total including NLEA", "Sugars, total"],
}


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


def _lookup_usda(query: str):
    api_key = os.environ.get("USDA_API_KEY")
    if not api_key:
        return None

    # Prefer generic/whole-food data types over Branded products, since a
    # plain query like "chicken breast" should match the unprocessed food,
    # not some specific packaged/prepared item that happens to share the name.
    # The key goes in a header, not the query string, so it never appears in
    # request URLs (httpx logs URLs at INFO, and Render keeps those logs).
    headers = {"X-Api-Key": api_key}
    data = _get_json(
        "USDA", USDA_SEARCH_URL,
        params={
            "query": query,
            "pageSize": 5,
            "dataType": ["Foundation", "SR Legacy"],
        },
        headers=headers,
        timeout=10,
    )
    if data is None:
        return None
    foods = data.get("foods", [])

    if not foods:
        # Fall back to any data type (including Branded) if nothing generic matched.
        data = _get_json(
            "USDA", USDA_SEARCH_URL,
            params={"query": query, "pageSize": 1},
            headers=headers,
            timeout=10,
        )
        if data is None:
            return None
        foods = data.get("foods", [])

    if not foods:
        return None

    food = foods[0]
    nutrients_by_name = {
        n.get("nutrientName"): n.get("value")
        for n in food.get("foodNutrients", [])
    }

    per_100g = {}
    for our_key, aliases in USDA_NUTRIENT_ALIASES.items():
        for alias in aliases:
            if alias in nutrients_by_name and nutrients_by_name[alias] is not None:
                per_100g[our_key] = float(nutrients_by_name[alias])
                break

    if "calories" not in per_100g:
        return None  # not usable without at least calories

    return {
        "per_100g": per_100g,
        "matched_name": food.get("description", query),
        "source": "usda",
    }


def _lookup_off(query: str):
    data = _get_json(
        "Open Food Facts", OFF_SEARCH_URL,
        params={
            "search_terms": query,
            "search_simple": 1,
            "action": "process",
            "json": 1,
            "page_size": 1,
        },
        timeout=10,
        headers={"User-Agent": "claude-fit-logger/1.0"},
    )
    if data is None:
        return None
    products = data.get("products", [])
    if not products:
        return None

    product = products[0]
    n = product.get("nutriments", {})

    if "energy-kcal_100g" not in n:
        return None

    per_100g = {
        "calories": n.get("energy-kcal_100g"),
        "protein_g": n.get("proteins_100g", 0),
        "carbs_g": n.get("carbohydrates_100g", 0),
        "fat_g": n.get("fat_100g", 0),
        "fiber_g": n.get("fiber_100g", 0),
        "sugar_g": n.get("sugars_100g", 0),
    }
    per_100g = {k: v for k, v in per_100g.items() if v is not None}

    return {
        "per_100g": per_100g,
        "matched_name": product.get("product_name", query),
        "source": "off",
    }


def _safe(source: str, fn, query: str):
    """Last-resort guard: an unexpected parsing error in one source counts as
    no match instead of crashing the tool call."""
    try:
        return fn(query)
    except Exception as e:
        log.warning("%s lookup crashed: %s", source, type(e).__name__)
        return None


def lookup_nutrition(item_name: str, quantity_g: float):
    """
    Returns a dict of nutrients scaled to quantity_g, or None if not found
    in either source.

    {
        "calories": ..., "protein_g": ..., "carbs_g": ..., "fat_g": ...,
        "fiber_g": ..., "sugar_g": ...,
        "matched_name": "...", "source": "usda" | "off"
    }
    """
    result = _safe("USDA", _lookup_usda, item_name) or _safe("Open Food Facts", _lookup_off, item_name)
    if result is None:
        return None

    scale = quantity_g / 100.0
    scaled = {k: round(v * scale, 1) for k, v in result["per_100g"].items()}
    scaled["matched_name"] = result["matched_name"]
    scaled["source"] = result["source"]
    return scaled
