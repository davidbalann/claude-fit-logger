"""
Entry-tracking store, backed by Neon Postgres.

The Google Health API returns a resource name for each created nutrition
log entry. We store it here so 'undo' and 'remove X' know exactly which
entry to delete, and so 'what's my total today' can be answered without
re-querying Google every time.
"""

import os
import psycopg
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DATABASE_URL = os.environ["DATABASE_URL"]  # Neon connection string

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id SERIAL PRIMARY KEY,
    item_name TEXT NOT NULL,
    quantity_g NUMERIC,
    calories NUMERIC NOT NULL,
    protein_g NUMERIC DEFAULT 0,
    carbs_g NUMERIC DEFAULT 0,
    fat_g NUMERIC DEFAULT 0,
    fiber_g NUMERIC DEFAULT 0,
    sugar_g NUMERIC DEFAULT 0,
    source TEXT NOT NULL,              -- 'usda' | 'off' | 'claude_estimate'
    estimated BOOLEAN DEFAULT FALSE,
    datapoint_name TEXT,
    logged_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _conn():
    return psycopg.connect(DATABASE_URL, autocommit=True)


def init_db():
    with _conn() as conn:
        conn.execute(SCHEMA)


def insert_entry(
    item_name, quantity_g, calories, protein_g, carbs_g, fat_g,
    fiber_g, sugar_g, source, estimated, datapoint_name,
):
    with _conn() as conn:
        row = conn.execute(
            """
            INSERT INTO entries
                (item_name, quantity_g, calories, protein_g, carbs_g,
                 fat_g, fiber_g, sugar_g, source, estimated,
                 datapoint_name)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (item_name, quantity_g, calories, protein_g, carbs_g,
             fat_g, fiber_g, sugar_g, source, estimated,
             datapoint_name),
        ).fetchone()
        return row[0]


def _today_bounds(tz_name: str):
    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def get_today_entries(tz_name: str):
    start_utc, end_utc = _today_bounds(tz_name)
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, item_name, quantity_g, calories, protein_g, carbs_g,
                   fat_g, fiber_g, sugar_g, source, estimated, logged_at
            FROM entries
            WHERE logged_at BETWEEN %s AND %s
            ORDER BY logged_at ASC
            """,
            (start_utc, end_utc),
        ).fetchall()
        cols = ["id", "item_name", "quantity_g", "calories", "protein_g",
                "carbs_g", "fat_g", "fiber_g", "sugar_g", "source",
                "estimated", "logged_at"]
        return [dict(zip(cols, r)) for r in rows]


def get_entry(entry_id: int):
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, item_name, datapoint_name FROM entries WHERE id = %s",
            (entry_id,),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "item_name": row[1], "datapoint_name": row[2]}


def get_most_recent_entry(tz_name: str):
    start_utc, end_utc = _today_bounds(tz_name)
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT id, item_name, datapoint_name
            FROM entries
            WHERE logged_at BETWEEN %s AND %s
            ORDER BY logged_at DESC LIMIT 1
            """,
            (start_utc, end_utc),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "item_name": row[1], "datapoint_name": row[2]}


def find_entry_by_name(item_name: str, tz_name: str):
    start_utc, end_utc = _today_bounds(tz_name)
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT id, item_name, datapoint_name
            FROM entries
            WHERE logged_at BETWEEN %s AND %s AND item_name ILIKE %s
            ORDER BY logged_at DESC LIMIT 1
            """,
            (start_utc, end_utc, f"%{item_name}%"),
        ).fetchone()
        if not row:
            return None
        return {"id": row[0], "item_name": row[1], "datapoint_name": row[2]}


def delete_entry(entry_id: int):
    with _conn() as conn:
        conn.execute("DELETE FROM entries WHERE id = %s", (entry_id,))
