"""
TODAY'S VALUE BOARD — one ranked report of where the value is on today's slate.

Merges, per game/side, every live signal we have:
  * SHARP lean   — sharp books price the side higher than soft books (de-vig divergence)
  * STEAM        — the line moved toward the side across books
  * X-VENUE      — Kalshi vs the commercial-book consensus disagree (independent venue)
  * PATTERN      — matches the proven "enter at the open on steam-up sides" edge
and ranks the slate by a transparent value score (sum of the confirming signals).

    python -m backtest.value_board [--min 0.02] [--top 15]

Pre-req: run `sharp_tracker.py` (sharp + steam) and `prediction_markets` (Kalshi)
earlier today so the signals exist.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

import db
import cross_venue


def _games() -> dict:
    return {g["game_pk"]: (g.get("away_team"), g.get("home_team"))
            for g in db.select("games", "?select=game_pk,away_team,home_team")}


def _sharp_today() -> list[dict]:
    # most-recent sharp signal per (game, market, selection)
    rows = db.select("sharp_signals",
                     "?select=game_pk,market_type,selection,divergence,steam_flag,"
                     "sharp_novig_prob,soft_novig_prob,line_delta,snapshot_time"
                     "&order=snapshot_time.desc&limit=500")
    seen, out = set(), []
    for r in rows:
        k = (r["game_pk"], r["market_type"], r["selection"])
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def run(min_edge: float = 0.02, top: int = 15):
    games = _games()
    rows = defaultdict(lambda: {"signals": [], "score": 0.0, "sharp": None,
                                "steam": False, "xv": None, "move": None})

    # sharp + steam + movement
    for s in _sharp_today():
        div = s.get("divergence") or 0
        if div < min_edge:
            continue
        key = (s["game_pk"], s["market_type"], s["selection"])
        r = rows[key]
        r["sharp"] = div
        r["steam"] = bool(s.get("steam_flag"))
        r["move"] = s.get("line_delta")
        r["score"] += div + (0.02 if s.get("steam_flag") else 0)
        r["signals"].append(f"sharp +{div*100:.1f}")
        if s.get("steam_flag"):
            r["signals"].append("STEAM")

    # cross-venue value (Kalshi vs commercial consensus), ML
    try:
        _g, _arbs, values, _thins = cross_venue.compute(min_edge=min_edge)
    except SystemExit:
        values = []
    for (gpk, sel, venue, edge, take, ref, vol, suspect) in values:
        if suspect:
            continue
        key = (gpk, "ml", sel)
        r = rows[key]
        r["xv"] = (venue, edge)
        r["score"] += edge
        r["signals"].append(f"x-venue +{edge*100:.1f} ({venue})")

    if not rows:
        print("\n  No value signals yet today. Run sharp_tracker.py and "
              "prediction_markets first (pre-game), then re-run.\n")
        return

    ranked = sorted(rows.items(), key=lambda kv: -kv[1]["score"])
    print(f"\n  TODAY'S VALUE BOARD  (ranked by combined signal; min {min_edge*100:.0f}%)")
    print(f"  {'GAME':<12}{'MKT':<9}{'SIDE':<7}{'SCORE':>7}   WHY")
    for (gpk, mkt, sel), r in ranked[:top]:
        away, home = games.get(gpk, ("?", "?"))
        label = f"{away}@{home}"
        why = " | ".join(r["signals"])
        n_conf = len([s for s in r["signals"] if s != "STEAM"])
        star = " **" if n_conf >= 2 else ""        # 2+ independent confirmations
        print(f"  {label:<12}{mkt:<9}{sel:<7}{r['score']*100:>6.1f}{star}   {why}")
    print("\n  SCORE = sharp divergence + x-venue value (+steam bonus). ** = 2+ independent")
    print("  confirmations (sharp AND another venue agree) — the highest-conviction value.")
    print("  Cross-check the top names against the proven pattern: best when the line is")
    print("  moving UP toward the side and you can enter near the open.\n")


def main():
    p = argparse.ArgumentParser(description="Today's value board — ranked slate value.")
    p.add_argument("--min", type=float, default=0.02, dest="min_edge")
    p.add_argument("--top", type=int, default=15)
    a = p.parse_args()
    run(a.min_edge, a.top)


if __name__ == "__main__":
    main()
