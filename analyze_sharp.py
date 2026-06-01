"""
Phase 5 (sharp) — read the sharp track record and answer:
WHICH sharp book to respect, in WHICH market, at WHAT time, under WHAT conditions.

Reads the Supabase performance views (which aggregate settled sharp observations).
Reports books/segments with a minimum sample, ranked by win rate.

    python -m backtest.analyze_sharp [--min-n 20]
"""

from __future__ import annotations

import argparse

import db


def run(min_n: int = 20):
    books = db.select("v_sharp_book_performance", "?order=win_rate.desc")
    seg = db.select("v_sharp_performance", "?order=win_rate.desc")
    settled = db.count("sharp_observations")

    print(f"\n  SHARP TRACK RECORD  (settled observations: {settled})")
    if not books:
        print("  No settled sharp observations yet. The track record fills as the")
        print("  tracker runs pre-game daily and games settle. Run pre-game, then:")
        print("    python -m backtest.import_outcomes && python -m backtest.settle_sharp\n")
        return

    print(f"\n  WHICH BOOK TO RESPECT (min sample {min_n}):")
    print(f"  {'BOOK':<14}{'N':>5}{'WINS':>6}{'WIN%':>7}{'AVG DIV':>9}")
    for b in books:
        flag = "" if b["n"] >= min_n else "  (low sample)"
        print(f"  {b['book']:<14}{b['n']:>5}{b['wins']:>6}{b['win_rate']*100:>6.1f}%"
              f"{b['avg_divergence']*100:>8.1f}{flag}")

    print(f"\n  BEST SEGMENTS — book x market x time x side (min sample {min_n}):")
    print(f"  {'BOOK':<12}{'MARKET':<11}{'TIME':<9}{'SIDE':<6}{'N':>4}{'WIN%':>7}")
    shown = 0
    for s in seg:
        if s["n"] < min_n:
            continue
        print(f"  {s['book']:<12}{s['market_type']:<11}{s['time_bucket']:<9}"
              f"{s['side_role']:<6}{s['n']:>4}{s['win_rate']*100:>6.1f}%")
        shown += 1
    if shown == 0:
        print("  (no segment has reached the minimum sample yet)")
    print()


def main():
    p = argparse.ArgumentParser(description="Sharp money track-record analysis.")
    p.add_argument("--min-n", type=int, default=20, help="Min sample to trust a row")
    run(p.parse_args().min_n)


if __name__ == "__main__":
    main()
