"""
Print projection accuracy summary from Supabase (or local reconcile cache).

    python scripts/projection_report.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config


def run() -> None:
    if not config.SUPABASE_URL or not config.SUPABASE_KEY:
        print("SUPABASE_URL / SUPABASE_KEY not set — run migration 0002_projection_tracking.sql first.")
        return

    url = (
        f"{config.SUPABASE_URL.rstrip('/')}/rest/v1/v_projection_accuracy_summary"
        "?order=prop_type"
    )
    req = urllib.request.Request(url, headers={
        "apikey": config.SUPABASE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        rows = json.loads(resp.read().decode())

    if not rows:
        print("No accuracy rows yet — push projections, then reconcile after games complete.")
        return

    print(f"\n{'Prop':<8} {'N':>5} {'MAE':>8} {'LeanHit':>10} {'Brier':>8}")
    print("-" * 42)
    for r in rows:
        print(
            f"{r.get('prop_type','?'):<8} {r.get('n',0):>5} "
            f"{r.get('mae') or 0:>8.3f} "
            f"{(r.get('lean_hit_rate') or 0)*100:>9.1f}% "
            f"{r.get('avg_brier') or 0:>8.4f}"
        )
    print()


if __name__ == "__main__":
    run()
