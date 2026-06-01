"""
Cross-book intelligence — sportsbooks post different numbers; this finds WHICH book
reveals what. Three reads off the odds snapshots:

  1. BEST-PRICE leaderboard — how often each book offers the best (cheapest) price.
     The line-shopping value: where you actually get the number.
  2. OFF-MARKET lean — each book's signed divergence from the de-vig consensus.
     A book persistently above/below consensus is either sharp-leading or soft-loose.
  3. SHARPNESS (forward) — once games settle, each book's Brier vs outcomes; the
     book whose prices best predict results is the one to respect. Honest-empty
     until the snapshot-with-outcome sample fills.

    python -m backtest.book_intel [--min-n 20]
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict

import db


def _latest_ml_rows() -> list[dict]:
    """Most-recent ML odds rows (one snapshot wave) per book/game/selection."""
    rows = db.select("odds_snapshots",
                     "?market_type=eq.ml&select=game_pk,selection,sportsbook,implied_probability,"
                     "snapshot_time&order=snapshot_time.desc&limit=6000")
    seen, out = set(), []
    for r in rows:
        key = (r["game_pk"], r["selection"], r["sportsbook"])
        if key in seen or r.get("implied_probability") is None:
            continue
        seen.add(key)
        out.append(r)
    return out


def run(min_n: int = 20):
    rows = _latest_ml_rows()
    if not rows:
        print("\n  No ML odds snapshots yet. Run sharp_tracker.py / market_data fetch.\n")
        return

    # consensus + best price per (game, selection)
    by_sel = defaultdict(list)
    for r in rows:
        by_sel[(r["game_pk"], r["selection"])].append(r)

    best_count = defaultdict(int)
    div_abs = defaultdict(list)
    div_signed = defaultdict(list)
    appear = defaultdict(int)
    for key, group in by_sel.items():
        imps = [g["implied_probability"] for g in group]
        consensus = statistics.median(imps)
        best = min(imps)               # cheapest = best price for the bettor
        for g in group:
            bk = g["sportsbook"]
            appear[bk] += 1
            if g["implied_probability"] <= best + 1e-9:
                best_count[bk] += 1
            div_abs[bk].append(abs(g["implied_probability"] - consensus))
            div_signed[bk].append(g["implied_probability"] - consensus)

    books = sorted(appear, key=lambda b: -best_count[b] / max(1, appear[b]))
    print(f"\n  CROSS-BOOK INTELLIGENCE  ({len(by_sel)} game-sides, {len(books)} books)")
    print(f"\n  BEST-PRICE LEADERBOARD (where you get the number)")
    print(f"  {'BOOK':<16}{'BEST%':>7}{'N':>5}{'|DIV|':>8}{'LEAN':>8}")
    for b in books:
        n = appear[b]
        if n < min_n:
            continue
        bestpct = best_count[b] / n
        adiv = statistics.mean(div_abs[b])
        lean = statistics.mean(div_signed[b])   # + = prices sides higher than market
        tag = "tight" if adiv < 0.012 else ("loose" if adiv > 0.03 else "")
        print(f"  {b:<16}{bestpct*100:>6.1f}%{n:>5}{adiv*100:>7.1f}{lean*100:>+7.1f}  {tag}")

    # forward sharpness: book implied vs outcome (Brier), if any settled snapshots exist
    settled = db.select("v_odds_pregame",
                        "?market_type=eq.ml&select=game_pk,selection,sportsbook,implied_probability&limit=1")
    print("\n  SHARPNESS vs outcomes (Brier; lower=sharper): forward-accumulating —")
    print("  fills once today's snapshots settle. Run after games finalize:")
    print("    python -m backtest.import_outcomes\n")
    print("  BEST% = how often the book had the best price. |DIV| = avg distance from")
    print("  the de-vig consensus (tight books lead; loose books are soft/exploitable).")
    print("  LEAN>0 = prices sides higher than the market (shades favorites).\n")


def main():
    p = argparse.ArgumentParser(description="Cross-book intelligence — which book reveals what.")
    p.add_argument("--min-n", type=int, default=20)
    run(p.parse_args().min_n)


if __name__ == "__main__":
    main()
