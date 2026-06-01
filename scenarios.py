"""
React-scenario evaluator — the sharp tool's intelligence layer.

Define a line-movement / liquidity / market scenario and get its HISTORICAL
consistency from the settled sample: sample size, win rate, the closing price's
own implied prob, the edge over the close, and the Wilson 95% LOWER BOUND on that
edge. A scenario is "objective" only when the lower bound still beats the closing
price — i.e., the market is provably (not by luck) mispricing that situation.

    python -m backtest.scenarios            # run the preset scenario board
    python -m backtest.scenarios --min-n 30

Programmatic:
    from scenarios import evaluate
    evaluate(move="up", move_min=0.01, liq_min=100000, market="ml")
"""

from __future__ import annotations

import argparse
import math

import db

Z = 1.96


def _wilson_lower(wins: int, n: int) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    return (p + Z * Z / (2 * n) - Z * math.sqrt((p * (1 - p) + Z * Z / (4 * n)) / n)) / (1 + Z * Z / n)


def evaluate(move: str | None = None, move_min: float = 0.01,
             liq_min: float = 0, liq_max: float | None = None,
             market: str = "ml", min_n: int = 20) -> dict:
    """Historical consistency of a scenario. `move` in {up, down, flat, None}."""
    q = ("?settled=eq.true&won=not.is.null&open_prob=not.is.null"
         f"&market_type=eq.{market}&select=implied_probability,delta,won,volume")
    if liq_min:
        q += f"&volume=gte.{liq_min}"
    if liq_max is not None:
        q += f"&volume=lte.{liq_max}"
    rows = db.select("prediction_market_snapshots", q)

    def keep(r):
        d = r.get("delta") or 0.0
        if move == "up":
            return d >= move_min
        if move == "down":
            return d <= -move_min
        if move == "flat":
            return abs(d) < move_min
        return True

    sel = [r for r in rows if keep(r)]
    n = len(sel)
    if n < min_n:
        return {"n": n, "enough": False}
    wins = sum(1 for r in sel if r["won"])
    win_rate = wins / n
    avg_close = sum(r["implied_probability"] for r in sel) / n
    floor = _wilson_lower(wins, n)
    return {
        "n": n, "enough": True, "win_rate": round(win_rate, 4),
        "avg_close": round(avg_close, 4),
        "edge_vs_close": round(win_rate - avg_close, 4),
        "win_rate_floor": round(floor, 4),
        "proven_edge": round(floor - avg_close, 4),     # >0 => objective edge
        "objective": (floor - avg_close) > 0,
    }


# Preset "react scenarios" — the situations worth knowing the track record of.
PRESETS = [
    ("steam up (any vol)",        dict(move="up", move_min=0.02)),
    ("steam up + liquid >100k",   dict(move="up", move_min=0.02, liq_min=100000)),
    ("drift up small",            dict(move="up", move_min=0.01, liq_max=100000)),
    ("flat line",                 dict(move="flat", move_min=0.01)),
    ("bet down (reverse)",        dict(move="down", move_min=0.02)),
    ("bet down + liquid >100k",   dict(move="down", move_min=0.02, liq_min=100000)),
    ("high liquidity only >250k", dict(liq_min=250000)),
    ("thin market <80k",          dict(liq_max=80000)),
]


def run(min_n: int = 20):
    print(f"\n  REACT-SCENARIO BOARD  (settled ML sample; min n={min_n})")
    print(f"  {'SCENARIO':<26}{'N':>5}{'WIN%':>7}{'CLOSE%':>8}{'EDGE':>7}{'FLOOR-EDGE':>11}  OBJECTIVE")
    any_data = False
    for label, kw in PRESETS:
        r = evaluate(min_n=min_n, **kw)
        if not r["enough"]:
            print(f"  {label:<26}{r['n']:>5}  (insufficient sample)")
            continue
        any_data = True
        flag = "** YES **" if r["objective"] else "no"
        print(f"  {label:<26}{r['n']:>5}{r['win_rate']*100:>6.1f}%{r['avg_close']*100:>7.1f}%"
              f"{r['edge_vs_close']*100:>+6.1f}{r['proven_edge']*100:>+10.1f}  {flag}")
    if any_data:
        print("\n  EDGE = actual win% - closing implied%. FLOOR-EDGE = Wilson 95% lower")
        print("  bound minus closing%. OBJECTIVE = the floor still beats the close.\n")
    else:
        print("  No settled sample yet — backfill candlesticks / let games settle.\n")


def main():
    p = argparse.ArgumentParser(description="React-scenario evaluator (line movement / liquidity).")
    p.add_argument("--min-n", type=int, default=20)
    run(p.parse_args().min_n)


if __name__ == "__main__":
    main()
