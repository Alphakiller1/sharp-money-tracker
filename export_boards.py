"""
Write the Pitcher + General-Markets boards into docs/data.json (merging with the
existing Supabase-derived sharp-signal data so the dashboard has everything).

    python export_boards.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import board_analytics as ba

DOCS = Path(__file__).resolve().parent / "docs"
DATA = DOCS / "data.json"


def run():
    existing = {}
    if DATA.exists():
        try:
            existing = json.loads(DATA.read_text(encoding="utf-8"))
        except ValueError:
            existing = {}

    pitchers = ba.pitcher_board()
    markets = ba.market_board()

    existing["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing["pitchers"] = pitchers
    existing["markets"] = markets

    DOCS.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    today = [p for p in pitchers if p["today"]]
    print(f"  wrote {len(pitchers)} pitchers ({len(today)} starting today) + {len(markets)} games -> {DATA}")

    try:
        from push_projections import run as push_proj
        push_proj(from_file=True)
    except Exception as exc:
        print(f"  (projection push skipped: {exc})")


if __name__ == "__main__":
    run()
