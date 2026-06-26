"""
Upsert today's pitcher projections to Supabase after export_boards.

    python push_projections.py

Gracefully skips when SUPABASE_URL / SUPABASE_KEY are unset.
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import board_analytics as ba
import config
from _compat import slate_date

DOCS = Path(__file__).resolve().parent / "docs"
DATA = DOCS / "data.json"
TABLE = "projection_snapshots"


def _headers() -> dict[str, str]:
    key = config.SUPABASE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def _row_from_pitcher(p: dict, slate: str) -> dict:
    props = p.get("props") or {}
    return {
        "slate_date": slate,
        "pitcher_id": p.get("player_id"),
        "pitcher_name": p.get("name"),
        "team": p.get("team"),
        "opp": p.get("opp"),
        "hand": p.get("hand"),
        "model_version": props.get("model_version") or config.MODEL_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proj_k": (props.get("K") or {}).get("proj"),
        "proj_bb": (props.get("BB") or {}).get("proj"),
        "proj_er": (props.get("ER") or {}).get("proj"),
        "proj_outs": (props.get("Outs") or {}).get("proj"),
        "proj_f5_er": (props.get("F5") or {}).get("proj"),
        "lean_k": (props.get("K") or {}).get("lean"),
        "lean_bb": (props.get("BB") or {}).get("lean"),
        "lean_er": (props.get("ER") or {}).get("lean"),
        "lean_outs": (props.get("Outs") or {}).get("lean"),
        "lean_f5": (props.get("F5") or {}).get("lean"),
        "conviction_k": (props.get("K") or {}).get("conviction"),
        "conviction_bb": (props.get("BB") or {}).get("conviction"),
        "luck": p.get("luck"),
        "skill_era": p.get("skill_era"),
        "verdict": p.get("verdict"),
        "factors_json": p.get("factors") or [],
        "props_json": props,
    }


def upsert_rows(rows: list[dict]) -> None:
    if not rows:
        return
    body = json.dumps(rows, default=str).encode("utf-8")
    conflict = "slate_date,pitcher_name,team,model_version"
    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/{TABLE}"
        f"?on_conflict={conflict}"
    )
    req = urllib.request.Request(url, data=body, method="POST", headers=_headers())
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"upsert failed HTTP {resp.status}")


def run(*, from_file: bool = False) -> int:
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        print("WARNING: Skipping projection push — set SUPABASE_URL and SUPABASE_KEY in .env")
        return 0

    slate = slate_date() or datetime.now(timezone.utc).date().isoformat()
    if from_file and DATA.exists():
        data = json.loads(DATA.read_text(encoding="utf-8"))
        pitchers = [p for p in data.get("pitchers", []) if p.get("today")]
    else:
        pitchers = [p for p in ba.pitcher_board() if p.get("today")]

    rows = [_row_from_pitcher(p, slate) for p in pitchers]
    upsert_rows(rows)
    print(f"  Pushed {len(rows)} projection snapshots for {slate}")
    return len(rows)


if __name__ == "__main__":
    run()
