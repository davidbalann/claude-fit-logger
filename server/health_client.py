"""
Google Health API (v4) client — replaces the legacy Google Fit client.

Writes nutrition logs as "anonymous food" entries (foodDisplayName + manual
nutrient values), reads them back, and deletes them by resource name.

Notes on the schema, learned from Google's docs + empirical probing:
  - energy / totalCarbohydrate / totalFat are TOP-LEVEL fields.
  - protein, fiber, sugar go in the `nutrients` array, using the enum names
    PROTEIN, DIETARY_FIBER, SUGAR (confirmed accepted by the API).
  - startUtcOffset / endUtcOffset are REQUIRED. Omitting them makes Google
    assume UTC+0, which puts the entry on the wrong local day.
  - Anonymous-food logs cannot be edited after creation, only deleted and
    re-created. That's fine — our undo/remove flow deletes.

Required env vars:
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN   (must be minted with the googlehealth.* scopes)
"""

import os
import time
import httpx
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TOKEN_URL = "https://oauth2.googleapis.com/token"
BASE = "https://health.googleapis.com/v4/users/me/dataTypes/nutrition-log/dataPoints"

# our field name -> Google Health Nutrient enum name
NUTRIENT_ENUM = {
    "protein_g": "PROTEIN",
    "fiber_g": "DIETARY_FIBER",
    "sugar_g": "SUGAR",
}

_cached_token = {"value": None, "expires_at": 0}


def _get_access_token() -> str:
    if _cached_token["value"] and time.time() < _cached_token["expires_at"] - 60:
        return _cached_token["value"]

    resp = httpx.post(TOKEN_URL, data={
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
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


def write_nutrition(nutrients: dict, item_name: str, tz_name: str,
                    meal_type: str = "MEAL_TYPE_UNSPECIFIED") -> str:
    """
    Writes one nutrition log entry at the current local time.

    nutrients: dict with keys calories, protein_g, carbs_g, fat_g, fiber_g, sugar_g
    tz_name:   IANA timezone, e.g. "America/Toronto"
    Returns the created data point's resource name (used later to delete it).
    """
    tz = ZoneInfo(tz_name)
    start_local = datetime.now(tz)
    end_local = start_local + timedelta(minutes=1)

    nutrient_list = []
    for our_key, enum_name in NUTRIENT_ENUM.items():
        val = nutrients.get(our_key)
        if val is not None:
            nutrient_list.append({
                "nutrient": enum_name,
                "quantity": {"grams": float(val)},
            })

    log = {
        "interval": {
            "startTime": start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "startUtcOffset": _offset_string(start_local),
            "endTime": end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endUtcOffset": _offset_string(end_local),
        },
        "foodDisplayName": item_name,
        "mealType": meal_type,
        "energy": {"kcal": float(nutrients.get("calories", 0))},
        "totalCarbohydrate": {"grams": float(nutrients.get("carbs_g", 0) or 0)},
        "totalFat": {"grams": float(nutrients.get("fat_g", 0) or 0)},
        "serving": {"amount": 1.0},
    }
    if nutrient_list:
        log["nutrients"] = nutrient_list

    r = httpx.post(BASE, headers=_headers(), json={"nutritionLog": log}, timeout=20)
    r.raise_for_status()
    return r.json().get("response", {}).get("name", "")


def delete_nutrition(datapoint_name: str):
    """Deletes a single nutrition log entry by its resource name."""
    if not datapoint_name:
        return
    r = httpx.post(f"{BASE}:batchDelete", headers=_headers(),
                   json={"names": [datapoint_name]}, timeout=20)
    if r.status_code not in (200, 404):
        r.raise_for_status()


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
    r.raise_for_status()
    return r.json()
