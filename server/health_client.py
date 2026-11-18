"""
Google Health API (v4) client, replaces the legacy Google Fit client.

Writes nutrition logs as "anonymous food" entries (foodDisplayName + manual
nutrient values), reads them back, and deletes them by resource name.

Notes on the schema, learned from Google's docs + empirical probing:
  - energy / totalCarbohydrate / totalFat are TOP-LEVEL fields.
  - protein, fiber, sugar go in the `nutrients` array, using the enum names
    PROTEIN, DIETARY_FIBER, SUGAR (confirmed accepted by the API).
  - startUtcOffset / endUtcOffset are REQUIRED. Omitting them makes Google
    assume UTC+0, which puts the entry on the wrong local day.
  - serving.foodMeasurementUnit is REQUIRED (observed 2026-11-17: HTTP 400
    "Required field 'serving.food_measurement_unit' is missing."). Google's
    nutrition guide still shows an anonymous-food example with only
    serving.amount, so the docs and the live API disagree. The value is a
    string; the guide's identified-food example shows a resource name of the
    form users/me/dataTypes/food-measurement-unit/dataPoints/<id>. [uncertain:
    that the same format applies to anonymous foods, and the response shape of
    the unit list, which is why _resolve_gram_unit logs the list on failure.]
  - mealType must be one of BREAKFAST, LUNCH, DINNER, SNACK. We never send
    MEAL_TYPE_UNSPECIFIED.
  - Anonymous-food logs cannot be edited after creation, only deleted and
    re-created. That's fine, our undo/remove flow deletes.

Required env vars:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN   (must be minted with the googlehealth.* scopes)
"""

import json
import logging
import os
import time
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger("claude-fit-logger.health")

TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE = "https://health.googleapis.com/v4/users/me/dataTypes/nutrition-log/dataPoints"
UNIT_BASE = "https://health.googleapis.com/v4/users/me/dataTypes/food-measurement-unit/dataPoints"

# our field name -> Google Health Nutrient enum name
NUTRIENT_ENUM = {
    "protein_g": "PROTEIN",
    "fiber_g": "DIETARY_FIBER",
    "sugar_g": "SUGAR",
}

MEAL_TYPES = ("BREAKFAST", "LUNCH", "DINNER", "SNACK")

# Google answers short outages with 503 UNAVAILABLE ("retry with a backoff").
# GET calls retry on any transient status; the nutrition write retries on 503
# only (UNAVAILABLE means the request was not processed, so a retry should not
# create a duplicate entry; a small risk remains if a 503 arrives after Google
# actually stored the entry).
TRANSIENT_STATUSES = (429, 500, 502, 503, 504)
RETRY_DELAYS_S = (1.0, 2.0)        # GET calls: up to 3 attempts
WRITE_RETRY_DELAYS_S = (2.0, 4.0)  # nutrition write: up to 3 attempts, 503 only
MAX_UNIT_PAGES = 50

_cached_token = {"value": None, "expires_at": 0}
_unit_cache = {}


class GoogleHealthError(RuntimeError):
    """Raised on any non-2xx from Google. The message includes Google's body
    so Claude sees the real reason in the tool error."""


def _raise_for_google(r: httpx.Response, action: str, payload=None):
    """Log and raise on non-2xx. Logs status, full response body and the JSON
    payload we sent. Never logs headers, tokens, secrets or the request URL
    query. `payload` is only ever the nutrition JSON (no credentials)."""
    if r.is_success:
        return
    body = r.text
    log.error("Google %s failed: status=%s", action, r.status_code)
    log.error("Google %s response body: %s", action, body)
    if payload is not None:
        log.error("Google %s payload sent: %s", action, json.dumps(payload, sort_keys=True))
    raise GoogleHealthError(
        f"Google {action} failed with HTTP {r.status_code}. Google's error body: {body}"
    )


def _send_with_retry(send, action: str, delays, statuses) -> httpx.Response:
    """Calls send() and retries after each delay while the response status is in
    `statuses`. Logs each retry at WARNING (status only, nothing sensitive).
    Returns the last response; the caller decides what to do with a failure."""
    r = send()
    for delay in delays:
        if r.status_code not in statuses:
            return r
        log.warning("Google %s returned HTTP %s, retrying in %.0fs", action, r.status_code, delay)
        time.sleep(delay)
        r = send()
    return r


def _get_access_token() -> str:
    if _cached_token["value"] and time.time() < _cached_token["expires_at"] - 60:
        return _cached_token["value"]

    resp = httpx.post(TOKEN_URL, data={
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=15)
    # Token endpoint error bodies (error, error_description) carry no secrets.
    _raise_for_google(resp, "token refresh")
    data = resp.json()
    _cached_token["value"] = data["access_token"]
    _cached_token["expires_at"] = time.time() + data["expires_in"]
    return _cached_token["value"]


def _headers():
    return {"Authorization": f"Bearer {_get_access_token()}",
            "Content-Type": "application/json"}


def _offset_string(dt: datetime) -> str:
    """Google wants the UTC offset as a duration string, e.g. '-14400s'."""
    offset = dt.utcoffset() or timedelta(0)
    return f"{int(offset.total_seconds())}s"


# --- meal type -------------------------------------------------------------

def infer_meal_type(hour: int) -> str:
    """Default meal type from the local hour (0-23). Never returns UNSPECIFIED."""
    if 5 <= hour < 11:
        return "BREAKFAST"
    if 11 <= hour < 15:
        return "LUNCH"
    if 17 <= hour < 21:
        return "DINNER"
    return "SNACK"  # 15:00-16:59 and 21:00-04:59


def resolve_meal_type(meal_type, now_local: datetime) -> str:
    """Validate an explicit meal_type, or infer one from the local hour."""
    if meal_type is None or str(meal_type).strip() == "":
        return infer_meal_type(now_local.hour)
    value = str(meal_type).strip().upper()
    if value not in MEAL_TYPES:
        raise ValueError(
            f"meal_type must be one of {', '.join(MEAL_TYPES)} (got {meal_type!r})"
        )
    return value


# --- food measurement unit (gram) ------------------------------------------

def _iter_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _find_gram_unit(data_points) -> str:
    """Pick the resource name of the 'gram' unit from a unit list. Returns ''
    if none matches. A data point matches when any string in it, or the last
    segment of its resource name, is exactly gram/grams/g (case-insensitive)."""
    wanted = {"gram", "grams", "g"}
    for dp in data_points or []:
        if not isinstance(dp, dict):
            continue
        name = dp.get("name") if isinstance(dp.get("name"), str) else ""
        candidates = [s.strip().lower() for s in _iter_strings(dp)]
        if name:
            candidates.append(name.rsplit("/", 1)[-1].strip().lower())
        if wanted.intersection(candidates) and name:
            return name
    return ""


def _resolve_gram_unit() -> str:
    """Looks up (and caches) the resource name of the 'gram' food measurement
    unit, required by serving.foodMeasurementUnit."""
    if _unit_cache.get("gram"):
        return _unit_cache["gram"]

    headers = _headers()
    data_points = []
    page_token = None
    for _ in range(MAX_UNIT_PAGES):
        params = {"pageToken": page_token} if page_token else None
        r = _send_with_retry(
            lambda: httpx.get(UNIT_BASE, headers=headers, params=params, timeout=20),
            "food measurement unit list", RETRY_DELAYS_S, TRANSIENT_STATUSES,
        )
        _raise_for_google(r, "food measurement unit list")
        body = r.json()
        page = body.get("dataPoints", [])
        data_points.extend(page)
        unit = _find_gram_unit(page)
        if unit:  # stop paging as soon as gram is found
            _unit_cache["gram"] = unit
            return unit
        page_token = body.get("nextPageToken")
        if not page_token:
            break

    unit = _find_gram_unit(data_points)
    if not unit:
        # Unit lists are static reference data (no secrets), safe to log.
        listing = json.dumps(data_points, sort_keys=True)[:6000]
        log.error("No 'gram' food measurement unit found. Unit list: %s", listing)
        raise GoogleHealthError(
            "Could not find a 'gram' food measurement unit in Google's unit list. "
            f"Unit list (truncated): {listing}"
        )
    _unit_cache["gram"] = unit
    return unit


# --- the one place the Google payload is built ----------------------------

def build_nutrition_payload(nutrients: dict, item_name: str, quantity_g: float,
                            now_local: datetime, meal_type, unit_name: str) -> dict:
    """
    Builds the JSON body for a nutrition-log write. This is the ONLY function
    that builds the Google payload; the server and its tests both use it.

    nutrients:  dict with keys calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g
    quantity_g: grams eaten, sent as serving.amount with the gram unit
    now_local:  timezone-aware local datetime of the entry
    meal_type:  BREAKFAST | LUNCH | DINNER | SNACK, or None to infer from the hour
    unit_name:  resource name of the 'gram' food measurement unit
    """
    if not unit_name:
        raise ValueError("unit_name is required (serving.foodMeasurementUnit)")
    if quantity_g is None or float(quantity_g) <= 0:
        raise ValueError("quantity_g must be greater than 0")

    meal = resolve_meal_type(meal_type, now_local)
    end_local = now_local + timedelta(minutes=1)

    nutrient_list = []
    for our_key, enum_name in NUTRIENT_ENUM.items():
        val = nutrients.get(our_key)
        if val is not None:
            nutrient_list.append({
                "nutrient": enum_name,
                "quantity": {"grams": float(val)},
            })

    nutrition_log = {
        "interval": {
            "startTime": now_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "startUtcOffset": _offset_string(now_local),
            "endTime": end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endUtcOffset": _offset_string(end_local),
        },
        "foodDisplayName": item_name,
        "mealType": meal,
        "energy": {"kcal": float(nutrients.get("calories", 0))},
        "totalCarbohydrate": {"grams": float(nutrients.get("carbs_g", 0) or 0)},
        "totalFat": {"grams": float(nutrients.get("fat_g", 0) or 0)},
        "serving": {
            "amount": float(quantity_g),
            "foodMeasurementUnit": unit_name,
        },
    }
    if nutrient_list:
        nutrition_log["nutrients"] = nutrient_list
    return {"nutritionLog": nutrition_log}


def write_nutrition(nutrients: dict, item_name: str, tz_name: str,
                    quantity_g: float, meal_type=None) -> str:
    """
    Writes one nutrition log entry at the current local time.

    nutrients: dict with keys calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g
    tz_name:   IANA timezone, e.g. "America/Toronto"
    meal_type: BREAKFAST | LUNCH | DINNER | SNACK, or None to infer from the hour
    Returns the created data point's resource name (used later to delete it).
    """
    now_local = datetime.now(ZoneInfo(tz_name))
    payload = build_nutrition_payload(
        nutrients, item_name, quantity_g, now_local, meal_type,
        _resolve_gram_unit(),
    )
    r = _send_with_retry(
        lambda: httpx.post(BASE, headers=_headers(), json=payload, timeout=20),
        "nutrition write", WRITE_RETRY_DELAYS_S, (503,),
    )
    if r.status_code == 400:
        _unit_cache.clear()  # in case a cached unit name went stale
    _raise_for_google(r, "nutrition write", payload)
    return r.json().get("response", {}).get("name", "")


def delete_nutrition(datapoint_name: str):
    """Deletes a single nutrition log entry by its resource name."""
    if not datapoint_name:
        return
    r = httpx.post(f"{BASE}:batchDelete", headers=_headers(),
                   json={"names": [datapoint_name]}, timeout=20)
    if r.status_code not in (200, 404):
        _raise_for_google(r, "nutrition delete", {"names": [datapoint_name]})


def list_nutrition_today(tz_name: str):
    """Reads today's nutrition log entries directly from Google Health.
    Normal reads go through our own Neon log instead (faster, and works even
    if Google is briefly unreachable); this is here as a cross-check."""
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)

    params = {
        "startTime": start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    r = httpx.get(BASE, headers=_headers(), params=params, timeout=20)
    _raise_for_google(r, "nutrition list")
    return r.json()
