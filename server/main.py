"""
Claude Fit Logger — MCP server entrypoint.

Exposes four tools to Claude:
    log_food      - look up (or accept precomputed) nutrition, write to Fit
    undo_entry    - remove the most recently logged item today
    remove_entry  - remove a specific item logged today, by name
    get_totals    - today's running nutrition totals

Required env vars:
    MCP_API_KEY           - shared secret Claude must send as X-API-Key
    DATABASE_URL           - Neon Postgres connection string
    GOOGLE_CLIENT_ID
    GOOGLE_CLIENT_SECRET
    GOOGLE_REFRESH_TOKEN
    USDA_API_KEY
    PORT                    - provided automatically by Render
"""

import os
import time
import logging

from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route, Mount

import db
import health_client
import nutrition_lookup
import oauth

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("claude-fit-logger")

NUTRIENT_KEYS = ["calories", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g"]

# --- one-time startup init -------------------------------------------------
db.init_db()

# --- MCP server + tools ------------------------------------------------------
mcp = FastMCP("claude-fit-logger")


def _compute_totals(tz: str) -> dict:
    rows = db.get_today_entries(tz)
    totals = {k: 0.0 for k in NUTRIENT_KEYS}
    for row in rows:
        for k in NUTRIENT_KEYS:
            totals[k] += float(row.get(k) or 0)
    totals = {k: round(v, 1) for k, v in totals.items()}
    totals["entry_count"] = len(rows)
    return totals


@mcp.tool
def log_food(
    item_name: str,
    quantity_g: float,
    tz: str,
    calories: float = None,
    protein_g: float = None,
    carbs_g: float = None,
    fat_g: float = None,
    fiber_g: float = None,
    sugar_g: float = None,
    meal_type: str = None,
    candidate_id: str = None,
) -> dict:
    """
    Log a food item to Google Health. Three ways to call it:

    1. Search: item_name, quantity_g, tz only. Returns status="candidates"
       with up to 5 options (USDA first, then Open Food Facts). Each has an
       id, name, source, data_type and per-100g calories/protein/carbs/fat/
       fiber/sugar. Nothing is logged. Pick the best match for what the user
       actually ate, then call again with that candidate's id.
    2. Log a candidate: same item_name/quantity_g/tz plus candidate_id copied
       from a candidates result. The server scales that food's per-100g values
       to quantity_g and logs it with its true source ("usda" or "off").
    3. Log your own numbers: item_name, quantity_g, tz plus calories (required)
       and any of protein_g/carbs_g/fat_g/fiber_g/sugar_g. Use this for label
       photos, restaurant items, or when status="not_found". Logged as
       source="claude_estimate". If candidate_id is also given, candidate_id wins.

    status="not_found" means no usable match (or the lookup sources were
    unreachable): estimate the numbers yourself and use way 3.

    meal_type is optional: BREAKFAST, LUNCH, DINNER or SNACK. If omitted it is
    inferred from the local hour in tz.

    tz must be an IANA timezone name, e.g. "America/Toronto".
    """
    if quantity_g is None or quantity_g <= 0:
        raise ValueError("quantity_g must be greater than 0")

    if candidate_id:
        candidate = nutrition_lookup.get_candidate(candidate_id)
        if candidate is None:
            return {
                "status": "not_found",
                "item_name": item_name,
                "quantity_g": quantity_g,
                "message": (
                    f"Could not load {candidate_id} (source unreachable or no kcal "
                    "value). Search again, or estimate calories (required) and "
                    "macros and call log_food with those values instead."
                ),
            }
        nutrients = nutrition_lookup.scale_candidate(candidate, quantity_g)
        source = candidate["source"]
        estimated = False
        matched_name = candidate["name"] or item_name
    elif calories is None:
        candidates = nutrition_lookup.search_candidates(item_name)
        if not candidates:
            return {
                "status": "not_found",
                "item_name": item_name,
                "quantity_g": quantity_g,
                "message": (
                    "No usable match in USDA or Open Food Facts (or both were "
                    "unreachable). Estimate calories (required) and macros, then "
                    "call log_food again with the same item_name/quantity_g/tz "
                    "plus those values."
                ),
            }
        return {
            "status": "candidates",
            "item_name": item_name,
            "quantity_g": quantity_g,
            "candidates": candidates,
            "message": (
                "Nothing was logged. Pick the best match and call log_food again "
                "with the same item_name/quantity_g/tz plus candidate_id. If none "
                "fits, estimate the numbers yourself and pass calories instead."
            ),
        }
    else:
        nutrients = {
            "calories": calories,
            "protein_g": protein_g or 0,
            "carbs_g": carbs_g or 0,
            "fat_g": fat_g or 0,
            "fiber_g": fiber_g or 0,
            "sugar_g": sugar_g or 0,
        }
        source = "claude_estimate"
        estimated = True
        matched_name = item_name

    datapoint_name = health_client.write_nutrition(
        nutrients, item_name=matched_name, tz_name=tz,
        quantity_g=quantity_g, meal_type=meal_type,
    )

    entry_id = db.insert_entry(
        item_name=item_name,
        quantity_g=quantity_g,
        calories=nutrients["calories"],
        protein_g=nutrients["protein_g"],
        carbs_g=nutrients["carbs_g"],
        fat_g=nutrients["fat_g"],
        fiber_g=nutrients["fiber_g"],
        sugar_g=nutrients["sugar_g"],
        source=source,
        estimated=estimated,
        datapoint_name=datapoint_name,
    )

    return {
        "status": "logged",
        "entry_id": entry_id,
        "matched_name": matched_name,
        "source": source,
        "estimated": estimated,
        "nutrients": nutrients,
        "today_totals": _compute_totals(tz),
    }


@mcp.tool
def undo_entry(tz: str) -> dict:
    """Removes the most recently logged item today, from both Google Health and the log."""
    entry = db.get_most_recent_entry(tz)
    if not entry:
        return {"status": "empty", "message": "No entries logged today."}

    health_client.delete_nutrition(entry["datapoint_name"])
    db.delete_entry(entry["id"])

    return {
        "status": "undone",
        "item_name": entry["item_name"],
        "today_totals": _compute_totals(tz),
    }


@mcp.tool
def remove_entry(item_name: str, tz: str) -> dict:
    """Removes a specific item logged today (matched by name), from both Google Health and the log."""
    entry = db.find_entry_by_name(item_name, tz)
    if not entry:
        return {"status": "not_found", "message": f"No entry matching '{item_name}' found today."}

    health_client.delete_nutrition(entry["datapoint_name"])
    db.delete_entry(entry["id"])

    return {
        "status": "removed",
        "item_name": entry["item_name"],
        "today_totals": _compute_totals(tz),
    }


@mcp.tool
def get_totals(tz: str) -> dict:
    """Returns today's running nutrition totals and the individual entries."""
    return {
        "status": "ok",
        "today_totals": _compute_totals(tz),
        "entries": db.get_today_entries(tz),
    }


# --- API key auth + ASGI wiring --------------------------------------------
API_KEY = os.environ["MCP_API_KEY"]


PUBLIC_BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")

EXEMPT_PATHS = {
    "/health",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/oauth/register",
    "/oauth/authorize",
    "/oauth/token",
}


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        auth_header = request.headers.get("Authorization", "")
        token_ok = auth_header == f"Bearer {API_KEY}"
        # Also accept the older header/query-param forms, for anything
        # other than Claude's own connector hitting this directly.
        if not token_ok:
            token_ok = (
                request.headers.get("X-API-Key") == API_KEY
                or request.query_params.get("key") == API_KEY
            )

        if not token_ok:
            resource_meta_url = f"{PUBLIC_BASE_URL}/.well-known/oauth-protected-resource"
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{resource_meta_url}"'},
            )
        return await call_next(request)


async def health(request):
    return PlainTextResponse("ok")


mcp_app = mcp.http_app(path="/", transport="http")

app = Starlette(
    routes=[
        Route("/health", health),
        Route("/.well-known/oauth-protected-resource", oauth.protected_resource_metadata),
        Route("/.well-known/oauth-authorization-server", oauth.authorization_server_metadata),
        Route("/oauth/register", oauth.register_client, methods=["POST"]),
        Route("/oauth/authorize", oauth.authorize),
        Route("/oauth/token", oauth.token, methods=["POST"]),
        Mount("/", app=mcp_app),
    ],
    middleware=[Middleware(ApiKeyMiddleware)],
    lifespan=mcp_app.lifespan,  # required for fastmcp session management when mounted
)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
