"""
Match projection snapshots to actual starter lines from sp_gamelog.csv.

    python scripts/reconcile_projection_outcomes.py [--date YYYY-MM-DD]

Updates projection_outcomes + projection_accuracy in Supabase.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config
from _compat import load

SNAP_TABLE = "projection_snapshots"
OUT_TABLE = "projection_outcomes"
ACC_TABLE = "projection_accuracy"


def _headers() -> dict[str, str]:
    key = config.SUPABASE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _get(path: str) -> list[dict]:
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{path}"
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _post(table: str, rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    url = f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{table}"
    body = json.dumps(rows, default=str).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={**_headers(), "Prefer": "resolution=merge-duplicates,return=representation"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _ip_to_outs(ip) -> float | None:
    try:
        s = str(ip)
        if "." in s:
            w, f = s.split(".")
            return int(w) * 3 + int(f)
        return float(s) * 3
    except (TypeError, ValueError):
        return None


def _lean_hit(lean: str, projected: float, actual: float) -> bool | None:
    if not lean or lean == "—":
        return None
    if lean == "over":
        return actual > projected
    if lean == "under":
        return actual < projected
    return None


def _brier(projected: float, actual: float, lean: str) -> float | None:
    if not lean or lean == "—":
        return None
    # binary: did actual exceed projection?
    event = 1.0 if actual > projected else 0.0
    prob = 0.58 if lean == "over" else 0.42
    return (prob - event) ** 2


def reconcile(target_date: str | None = None) -> int:
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        print("WARNING: Skipping reconcile — set SUPABASE_URL and SUPABASE_KEY")
        return 0

    gamelog = load("sp_gamelog.csv")
    if gamelog is None or gamelog.empty:
        print("No sp_gamelog.csv — nothing to reconcile")
        return 0

    q = SNAP_TABLE + "?select=*"
    if target_date:
        q += "&slate_date=eq." + urllib.parse.quote(target_date)
    else:
        q += "&order=slate_date.desc&limit=200"
    snaps = _get(q)
    if not snaps:
        print("No snapshots in Supabase")
        return 0

    gl = gamelog.copy()
    gl["date"] = gl["date"].astype(str).str[:10]
    n = 0
    for snap in snaps:
        slate = str(snap.get("slate_date", ""))[:10]
        name = str(snap.get("pitcher_name", "")).strip()
        sub = gl[(gl["date"] == slate) & (gl["pitcher_name"].astype(str).str.strip() == name)]
        if sub.empty:
            continue
        row = sub.iloc[0]
        ip = float(row.get("IP") or 0)
        outs = _ip_to_outs(ip)
        outcome = {
            "snapshot_id": snap["id"],
            "game_date": slate,
            "actual_k": int(row.get("K") or 0),
            "actual_bb": int(row.get("BB") or 0),
            "actual_er": int(row.get("ER") or 0),
            "actual_outs": outs,
            "actual_ip": ip,
            "actual_f5_er": float(row.get("f5_er") or 0) if pd.notna(row.get("f5_er")) else None,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }
        _post(OUT_TABLE, [outcome])

        acc_rows = []
        for prop, col_proj, col_actual in (
            ("K", "proj_k", "actual_k"),
            ("BB", "proj_bb", "actual_bb"),
            ("ER", "proj_er", "actual_er"),
            ("Outs", "proj_outs", "actual_outs"),
            ("F5", "proj_f5_er", "actual_f5_er"),
        ):
            proj = snap.get(col_proj)
            actual = outcome.get(col_actual)
            if proj is None or actual is None:
                continue
            proj_f, act_f = float(proj), float(actual)
            lean = snap.get(f"lean_{prop.lower()}") or snap.get(f"lean_{prop.lower().replace('5', 'f5')}")
            if prop == "F5":
                lean = snap.get("lean_f5")
            delta = act_f - proj_f
            acc_rows.append({
                "snapshot_id": snap["id"],
                "prop_type": prop,
                "projected": proj_f,
                "actual": act_f,
                "delta": round(delta, 3),
                "abs_error": round(abs(delta), 3),
                "lean": lean,
                "lean_hit": _lean_hit(str(lean or ""), proj_f, act_f),
                "brier": _brier(proj_f, act_f, str(lean or "")),
            })
        _post(ACC_TABLE, acc_rows)
        n += 1
        print(f"  reconciled {name} ({slate})")
    print(f"Done: {n} snapshots reconciled")
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="Slate date YYYY-MM-DD")
    args = p.parse_args()
    reconcile(args.date)


if __name__ == "__main__":
    main()
