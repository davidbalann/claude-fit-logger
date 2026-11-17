"""
Shared test setup. Tests run fully offline:
  - env vars that server/main.py reads at import time get dummy values
  - a fake in-memory `db` module replaces server/db.py (no Neon needed)
  - any real socket connection raises, so a test that forgets to fake the
    network fails loudly instead of silently going online
"""

import itertools
import os
import socket
import sys
import types
from pathlib import Path

import pytest

SERVER_DIR = str(Path(__file__).resolve().parent.parent / "server")
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

os.environ.setdefault("MCP_API_KEY", "test-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://example.test")
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")
os.environ.setdefault("USDA_API_KEY", "test-usda-key")


def _make_fake_db():
    fake = types.ModuleType("db")
    fake.rows = []
    counter = itertools.count(1)

    def init_db():
        pass

    def insert_entry(item_name, quantity_g, calories, protein_g, carbs_g, fat_g,
                     fiber_g, sugar_g, source, estimated, datapoint_name):
        row = dict(id=next(counter), item_name=item_name, quantity_g=quantity_g,
                   calories=calories, protein_g=protein_g, carbs_g=carbs_g,
                   fat_g=fat_g, fiber_g=fiber_g, sugar_g=sugar_g, source=source,
                   estimated=estimated, datapoint_name=datapoint_name)
        fake.rows.append(row)
        return row["id"]

    def get_today_entries(tz_name):
        return list(fake.rows)

    fake.init_db = init_db
    fake.insert_entry = insert_entry
    fake.get_today_entries = get_today_entries
    fake.get_most_recent_entry = lambda tz: fake.rows[-1] if fake.rows else None
    fake.find_entry_by_name = lambda name, tz: next(
        (r for r in reversed(fake.rows) if name.lower() in r["item_name"].lower()), None)
    fake.delete_entry = lambda entry_id: fake.rows.__setitem__(
        slice(None), [r for r in fake.rows if r["id"] != entry_id])
    return fake


sys.modules["db"] = _make_fake_db()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    import nutrition_lookup
    monkeypatch.setattr(nutrition_lookup, "RETRY_DELAY_S", 0)


@pytest.fixture(autouse=True)
def clean_db():
    sys.modules["db"].rows.clear()
