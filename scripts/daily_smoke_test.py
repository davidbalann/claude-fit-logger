"""
Daily smoke test for claude-fit-logger.

Calls the DEPLOYED server's real tools over MCP (the same path Claude uses),
so it fails whenever a real log_food call from Claude would fail. It holds no
copy of the Google payload and needs no Google credentials: the server uses
its own Render environment for Google and Neon.

Steps:
  1. /health responds (Render free tier cold start: 3 attempts x 90 s)
  2. MCP session opens with MCP_API_KEY
  3. log_food, manual-values path -> status "logged"; entry visible in get_totals
  4. log_food, lookup path -> "candidates", then log the first one by
     candidate_id -> status "logged" with a true source (usda/off)
  5. remove_entry for everything this test logged

A lookup-source outage alone (not_found) does NOT fail the test, because
Claude can still log with its own estimate. Any crash or unexpected result
anywhere else fails it, and the message names the layer:
  server reachability | Google token | lookup | Google write | Neon | cleanup

Required env vars (GitHub secrets):
    PUBLIC_BASE_URL
    MCP_API_KEY
"""

import json
import os
import sys
import time

import httpx

TZ = "America/Toronto"
LOOKUP_QUERY = "banana"

LAYER_REACH = "server reachability"
LAYER_TOKEN = "Google token"
LAYER_LOOKUP = "lookup"
LAYER_WRITE = "Google write"
LAYER_NEON = "Neon"
LAYER_CLEANUP = "cleanup"


class SmokeFailure(Exception):
    def __init__(self, layer: str, detail: str):
        super().__init__(f"[{layer}] {detail}")
        self.layer = layer
        self.detail = detail


def classify_tool_error(text: str, default_layer: str) -> str:
    """Maps a server tool error message to the layer that failed."""
    low = text.lower()
    if "token refresh" in low:
        return LAYER_TOKEN
    if "google" in low and ("nutrition write" in low or "food measurement unit" in low
                            or "failed with http" in low):
        return LAYER_WRITE
    neon_markers = ("psycopg", "operationalerror", "could not connect", "connection failed",
                    "connection refused", "server closed the connection",
                    "timeout expired", "could not translate host name", "database")
    if any(m in low for m in neon_markers):
        return LAYER_NEON
    return default_layer


# --- minimal MCP client (Streamable HTTP, JSON-RPC over POST) --------------

class McpClient:
    def __init__(self, base_url: str, api_key: str):
        self.url = base_url.rstrip("/") + "/"
        self.client = httpx.Client(timeout=120)
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._id = 0

    def _post(self, body: dict) -> httpx.Response:
        return self.client.post(self.url, headers=self.headers, json=body)

    @staticmethod
    def _parse(resp: httpx.Response, want_id: int) -> dict:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == want_id:
                        return msg
            raise ValueError("no JSON-RPC response with the expected id in the event stream")
        return resp.json()

    def initialize(self):
        self._id += 1
        resp = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                       "clientInfo": {"name": "fit-logger-smoke-test", "version": "1"}},
        })
        if resp.status_code != 200:
            raise SmokeFailure(LAYER_REACH, f"MCP initialize returned {resp.status_code}: {resp.text[:300]}")
        session_id = resp.headers.get("mcp-session-id")
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
        self._parse(resp, self._id)
        note = self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if note.status_code not in (200, 202):
            raise SmokeFailure(LAYER_REACH, f"MCP initialized notification returned {note.status_code}")

    def call_tool(self, name: str, arguments: dict):
        """Returns (is_error, data). data is the parsed tool result, or the error text."""
        self._id += 1
        resp = self._post({
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        if resp.status_code != 200:
            raise SmokeFailure(LAYER_REACH, f"tools/call {name} returned {resp.status_code}: {resp.text[:300]}")
        msg = self._parse(resp, self._id)
        if "error" in msg:
            return True, json.dumps(msg["error"])
        result = msg.get("result", {})
        text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
        if result.get("isError"):
            return True, text
        if isinstance(result.get("structuredContent"), dict):
            data = result["structuredContent"]
            return False, data.get("result", data) if set(data) == {"result"} else data
        try:
            return False, json.loads(text)
        except ValueError:
            return False, text


# --- steps -----------------------------------------------------------------

def check_server_health(base_url: str):
    # Render's free tier can take 30-60+ seconds to wake from a cold start.
    last_error = None
    for attempt in range(3):
        try:
            r = httpx.get(f"{base_url}/health", timeout=90)
            if r.status_code == 200 and r.text.strip() == "ok":
                print("OK: server is reachable")
                return
            last_error = f"/health returned {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_error = str(e)
        print(f"Attempt {attempt + 1} failed ({last_error}), retrying...")
    raise SmokeFailure(LAYER_REACH, f"Server unreachable after 3 attempts: {last_error}")


def call_or_fail(mcp: McpClient, tool: str, args: dict, default_layer: str) -> dict:
    is_error, data = mcp.call_tool(tool, args)
    if is_error:
        raise SmokeFailure(classify_tool_error(str(data), default_layer), f"{tool} error: {str(data)[:1500]}")
    if not isinstance(data, dict):
        raise SmokeFailure(default_layer, f"{tool} returned a non-object result: {str(data)[:300]}")
    return data


def manual_path(mcp: McpClient, stamp: str, logged: list):
    name = f"zz smoke test manual {stamp}"
    res = call_or_fail(mcp, "log_food", {
        "item_name": name, "quantity_g": 1, "tz": TZ, "calories": 1,
        "protein_g": 0, "carbs_g": 0, "fat_g": 0, "fiber_g": 0, "sugar_g": 0,
    }, LAYER_WRITE)
    if res.get("status") != "logged":
        raise SmokeFailure(LAYER_WRITE, f"manual log_food returned status {res.get('status')!r}: {json.dumps(res)[:500]}")
    logged.append(name)
    if res.get("source") != "claude_estimate" or res.get("estimated") is not True:
        raise SmokeFailure(LAYER_NEON, f"manual entry has wrong source/estimated: {json.dumps(res)[:500]}")
    if not isinstance(res.get("entry_id"), int):
        raise SmokeFailure(LAYER_NEON, f"manual entry has no entry_id: {json.dumps(res)[:500]}")

    totals = call_or_fail(mcp, "get_totals", {"tz": TZ}, LAYER_NEON)
    names = [e.get("item_name") for e in totals.get("entries", [])]
    if name not in names:
        raise SmokeFailure(LAYER_NEON, "manual entry was logged but is missing from get_totals")
    print("OK: manual-values log_food wrote to Google Health and Neon")


def lookup_path(mcp: McpClient, logged: list):
    res = call_or_fail(mcp, "log_food", {"item_name": LOOKUP_QUERY, "quantity_g": 1, "tz": TZ}, LAYER_LOOKUP)
    status = res.get("status")
    if status == "not_found":
        print("WARN: lookup returned not_found (a lookup source is down, or no match). "
              "Claude can still log with its own estimate, so this is not a failure.")
        return
    if status != "candidates":
        raise SmokeFailure(LAYER_LOOKUP, f"lookup log_food returned unexpected status {status!r}: {json.dumps(res)[:500]}")

    candidates = res.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 5:
        raise SmokeFailure(LAYER_LOOKUP, f"expected 1 to 5 candidates, got: {json.dumps(candidates)[:500]}")
    for c in candidates:
        per = c.get("per_100g") or {}
        if not (c.get("id") and c.get("name") and c.get("source") in ("usda", "off")
                and c.get("data_type") and isinstance(per.get("calories"), (int, float))):
            raise SmokeFailure(LAYER_LOOKUP, f"malformed candidate: {json.dumps(c)[:500]}")

    chosen = candidates[0]
    res2 = call_or_fail(mcp, "log_food", {
        "item_name": LOOKUP_QUERY, "quantity_g": 1, "tz": TZ, "candidate_id": chosen["id"],
    }, LAYER_WRITE)
    if res2.get("status") == "not_found":
        print("WARN: candidate could not be re-fetched (lookup source down). Not a failure.")
        return
    if res2.get("status") != "logged":
        raise SmokeFailure(LAYER_WRITE, f"candidate log_food returned status {res2.get('status')!r}: {json.dumps(res2)[:500]}")
    logged.append(LOOKUP_QUERY)
    if res2.get("source") != chosen["source"] or res2.get("estimated") is not False:
        raise SmokeFailure(LAYER_NEON, f"candidate entry has wrong source/estimated: {json.dumps(res2)[:500]}")
    print(f"OK: lookup path logged {chosen['id']} (source {res2['source']})")


def cleanup(mcp: McpClient, logged: list) -> list:
    failures = []
    for name in reversed(logged):
        try:
            is_error, data = mcp.call_tool("remove_entry", {"item_name": name, "tz": TZ})
            if is_error or not isinstance(data, dict) or data.get("status") != "removed":
                failures.append(SmokeFailure(LAYER_CLEANUP, f"remove_entry({name!r}) failed: {str(data)[:500]}"))
            else:
                print(f"OK: removed {name!r}")
        except Exception as e:
            failures.append(SmokeFailure(LAYER_CLEANUP, f"remove_entry({name!r}) crashed: {e}"))
    return failures


def main() -> int:
    base_url = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    api_key = os.environ["MCP_API_KEY"]
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    logged: list = []
    failures: list = []
    mcp = None

    try:
        check_server_health(base_url)
        mcp = McpClient(base_url, api_key)
        mcp.initialize()
        print("OK: MCP session opened")
        manual_path(mcp, stamp, logged)
        lookup_path(mcp, logged)
    except SmokeFailure as e:
        failures.append(e)
    except Exception as e:  # any crash anywhere fails the test
        failures.append(SmokeFailure(LAYER_REACH, f"unexpected {type(e).__name__}: {e}"))

    if mcp is not None and logged:
        failures.extend(cleanup(mcp, logged))

    if failures:
        for f in failures:
            print(f"FAIL [{f.layer}]: {f.detail}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
