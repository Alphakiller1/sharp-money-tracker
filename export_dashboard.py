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

import pandas as pd

import db
import market_edge
import cross_venue

DOCS = Path(__file__).resolve().parent / "docs"
SHARP_CSV = Path(__file__).resolve().parent / "data" / "sharp_signals.csv"


def _safe(view: str, params: str = "") -> list[dict]:
    try:
        return db.select(view, params)
    except SystemExit:
        return []


def _today_sharp_signals() -> list[dict]:
    """Return the current slate's signals, including locally queued rows.

    The tracker always writes its CSV before attempting Supabase.  Reading both
    sources keeps the live dashboard current if a warehouse FK/schema problem
    temporarily prevents the insert.
    """
    from _compat import TODAY

    fields = {
        "market_type", "selection", "divergence", "sharp_novig_prob",
        "soft_novig_prob", "steam_flag", "snapshot_time",
    }
    remote = _safe(
        "sharp_signals",
        "?select=market_type,selection,divergence,sharp_novig_prob,soft_novig_prob,"
        "steam_flag,snapshot_time&order=snapshot_time.desc&limit=100",
    )
    local: list[dict] = []
    if SHARP_CSV.exists():
        frame = pd.read_csv(SHARP_CSV)
        if "snapshot_time" in frame.columns:
            frame = frame[frame["snapshot_time"].astype(str).str.startswith(TODAY)]
            local = frame.to_dict(orient="records")

    rows = remote + local
    rows = [r for r in rows if str(r.get("snapshot_time", "")).startswith(TODAY)]
    unique: dict[tuple, dict] = {}
    for row in rows:
        clean = {k: v for k, v in row.items() if k in fields}
        key = (clean.get("snapshot_time"), clean.get("market_type"), clean.get("selection"))
        unique[key] = clean
    return sorted(unique.values(), key=lambda r: str(r.get("snapshot_time", "")), reverse=True)[:20]


def build() -> dict:
    edges = market_edge.scan(min_n=30, hurdle=0.03)
    tradeable = [e for e in edges if e.get("tradeable")]
    try:
        _games, arbs, values, thins = cross_venue.compute()
    except SystemExit:
        arbs, values, thins = [], [], []
    cv = {
        "arbs": [{"legs": l, "cost": c, "profit": p}
                 for (g, l, c, p) in sorted(arbs, key=lambda x: x[2]) if p <= 12][:10],
        "value": [{"sel": s, "venue": v, "edge": e, "vol": vol}
                  for (g, s, v, e, tk, rf, vol, susp) in sorted(values, key=lambda x: -x[3])
                  if not susp][:10],
        "thin": [{"sel": s, "div": d, "vol": vol}
                 for (g, s, d, vol) in sorted(thins, key=lambda x: abs(x[2]), reverse=True)][:10],
    }
    sharp_today = _today_sharp_signals()
    return {
        "cross_venue": cv,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "settled_contracts": db.count("prediction_market_snapshots"),
            "sharp_observations": db.count("sharp_observations"),
            "sharp_signals": len(sharp_today),
            "sharp_signals_total": db.count("sharp_signals"),
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
        "sharp_signals_recent": sharp_today,
        "sharp_performance": _safe("v_sharp_book_performance", "?order=win_rate.desc"),
    }


def main():
    from _compat import check_slate_freshness
    check_slate_freshness("the dashboard export")
    DOCS.mkdir(exist_ok=True)
    data = build()
    (DOCS / "data.json").write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"  Wrote {DOCS / 'data.json'}")
    print(f"  settled={data['counts']['settled_contracts']} "
          f"tradeable_edges={len(data['tradeable'])} "
          f"sharp_signals={data['counts']['sharp_signals']}")


if __name__ == "__main__":
    main()
