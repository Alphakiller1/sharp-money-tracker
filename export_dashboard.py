"""
Export the tracker's current intelligence to docs/data.json for the visual
dashboard (GitHub Pages). The dashboard is how the tool ARTICULATES its logic:
the pipeline, the quant gating, and the live findings — not just numbers.

    python export_dashboard.py
    # then: git add docs/data.json && commit && push  (Pages serves docs/)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import db
import market_edge

DOCS = Path(__file__).resolve().parent / "docs"


def _safe(view: str, params: str = "") -> list[dict]:
    try:
        return db.select(view, params)
    except SystemExit:
        return []


def build() -> dict:
    edges = market_edge.scan(min_n=30, hurdle=0.03)
    tradeable = [e for e in edges if e.get("tradeable")]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "settled_contracts": db.count("prediction_market_snapshots"),
            "sharp_observations": db.count("sharp_observations"),
            "sharp_signals": db.count("sharp_signals"),
            "games": db.count("games"),
        },
        "market_edge": [
            {k: (round(v, 4) if isinstance(v, float) else v) for k, v in e.items() if k != "p"}
            for e in edges if e.get("enough")
        ],
        "tradeable": [
            {"label": e["label"], "roi": round(e["roi"], 4), "roi_lb": round(e["roi_lb"], 4),
             "kelly": round(e["kelly"], 4), "clv": round(e["clv"], 4), "n": e["n"]}
            for e in sorted(tradeable, key=lambda x: -x["kelly"])
        ],
        "line_move_vs_outcome": _safe("v_line_move_vs_outcome", "?order=move_bucket.asc"),
        "open_vs_close": _safe("v_open_vs_close_brier"),
        "liquidity": _safe("v_liquidity_calibration", "?order=liq_q.asc"),
        "pm_calibration": _safe("v_pm_calibration", "?order=price_bucket.asc"),
        "sharp_signals_recent": _safe(
            "sharp_signals",
            "?select=market_type,selection,divergence,sharp_novig_prob,soft_novig_prob,steam_flag"
            "&order=snapshot_time.desc&limit=20"),
        "sharp_performance": _safe("v_sharp_book_performance", "?order=win_rate.desc"),
    }


def main():
    DOCS.mkdir(exist_ok=True)
    data = build()
    (DOCS / "data.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"  Wrote {DOCS / 'data.json'}")
    print(f"  settled={data['counts']['settled_contracts']} "
          f"tradeable_edges={len(data['tradeable'])} "
          f"sharp_signals={data['counts']['sharp_signals']}")


if __name__ == "__main__":
    main()
