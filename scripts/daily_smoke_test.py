"""
Daily smoke test for claude-fit-logger.

Checks the things that can silently break without the server itself going
down:
  1. The server is reachable (/health)
  2. The Google refresh token is still valid (this is what actually broke
     on 2026-08-31 — the server was "up" the whole time, only the token
     refresh was failing)
  3. A real write + delete against the Google Health API succeeds
     end-to-end (catches scope/permission regressions, not just auth)

Exits non-zero on any failure, with a clear reason printed — this is what
GitHub Actions uses to mark the workflow run as failed, which is what
triggers the email notification.
"""

import os
import sys
import httpx
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ["PUBLIC_BASE_URL"].rstrip("/")
CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]

HEALTH_API = "https://health.googleapis.com/v4/users/me/dataTypes/nutrition-log/dataPoints"


def fail(msg: str):
    print(f"FAIL: {msg}")
    sys.exit(1)


def check_server_health():
    # Render's free tier can take 30-60+ seconds to wake from a cold start.
    # Retry with a generous per-attempt timeout rather than failing on the
    # first slow response.
    last_error = None
    for attempt in range(3):
        try:
            r = httpx.get(f"{BASE_URL}/health", timeout=90)
            if r.status_code == 200 and r.text.strip() == "ok":
                print("OK: server is reachable")
                return
            last_error = f"/health returned {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_error = str(e)
        print(f"Attempt {attempt + 1} failed ({last_error}), retrying...")
    fail(f"Server unreachable after 3 attempts: {last_error}")


def get_access_token():
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }, timeout=15)
    if r.status_code != 200:
        fail(f"Google token refresh failed ({r.status_code}): {r.text[:300]}")
    print("OK: Google refresh token is valid")
    return r.json()["access_token"]


def check_health_api_round_trip(access_token: str):
    headers = {"Authorization": f"Bearer {access_token}",
               "Content-Type": "application/json"}
    start = datetime.now(timezone.utc)
    end = start + timedelta(minutes=1)

    body = {"nutritionLog": {
        "interval": {
            "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "startUtcOffset": "0s",
            "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endUtcOffset": "0s",
        },
        "foodDisplayName": "smoke test entry (auto-deleted)",
        "mealType": "SNACK",
        "energy": {"kcal": 1},
        "totalCarbohydrate": {"grams": 0},
        "totalFat": {"grams": 0},
    }}

    r = httpx.post(HEALTH_API, headers=headers, json=body, timeout=20)
    if r.status_code != 200:
        fail(f"Google Health API write failed ({r.status_code}): {r.text[:300]}")

    name = r.json().get("response", {}).get("name")
    print("OK: Google Health API write succeeded")

    if name:
        httpx.post(f"{HEALTH_API}:batchDelete", headers=headers,
                   json={"names": [name]}, timeout=20)
        print("OK: test entry cleaned up")


if __name__ == "__main__":
    check_server_health()
    token = get_access_token()
    check_health_api_round_trip(token)
    print("\nAll checks passed.")