"""
Minimal Supabase (PostgREST) client — insert/upsert with stdlib only.

Reads SUPABASE_URL + SUPABASE_KEY (service-role) from config/.env. Used by the
backtest importers so we don't add a heavy dependency.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import config

_BASE = config.SUPABASE_URL.rstrip("/") + "/rest/v1"


def _headers(extra: dict | None = None) -> dict:
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        raise SystemExit("SUPABASE_URL / SUPABASE_KEY not set (see .env).")
    h = {
        "apikey": config.SUPABASE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _post(table: str, rows: list[dict], params: str, prefer: str) -> int:
    if not rows:
        return 0
    sent = 0
    for i in range(0, len(rows), 500):                       # chunk
        chunk = rows[i:i + 500]
        url = f"{_BASE}/{table}{params}"
        data = json.dumps(chunk, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers=_headers({"Prefer": prefer}))
        try:
            with urllib.request.urlopen(req, timeout=40):
                sent += len(chunk)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:500]
            raise SystemExit(f"  {table} write failed (HTTP {e.code}): {body}") from e
    return sent


def insert(table: str, rows: list[dict]) -> int:
    """Plain insert (use for timestamped snapshots — each run is a new row)."""
    return _post(table, rows, "", "return=minimal")


def upsert(table: str, rows: list[dict], on_conflict: str) -> int:
    """Insert-or-update on the given conflict column(s)."""
    return _post(table, rows, f"?on_conflict={on_conflict}",
                 "resolution=merge-duplicates,return=minimal")


def select(table: str, params: str = "") -> list[dict]:
    """GET rows from a table/view. params e.g. '?select=game_pk&settled=eq.false'."""
    req = urllib.request.Request(f"{_BASE}/{table}{params}", headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"  {table} read failed (HTTP {e.code}): "
                         f"{e.read().decode('utf-8','ignore')[:300]}") from e


def count(table: str) -> int:
    """Exact row count for a table (HEAD with count=exact)."""
    req = urllib.request.Request(f"{_BASE}/{table}?select=*", method="HEAD",
                                 headers=_headers({"Prefer": "count=exact",
                                                   "Range-Unit": "items", "Range": "0-0"}))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            cr = r.headers.get("content-range", "*/0")        # e.g. 0-0/123
            return int(cr.split("/")[-1])
    except urllib.error.HTTPError:
        return -1
