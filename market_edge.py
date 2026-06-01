"""
Market-edge quant engine — find consistencies that translate to PROFIT, not just
probability points. Minute edges on the (efficient) closing line do not win money;
the engine therefore measures the things that actually compound a bankroll:

  * ROI at the ENTRY price (open) — you act before the move, so profit is realised
    against the price you could actually get, net of vig.
  * CLV (closing line value) = close - open — the single best-validated leading
    indicator of long-run sports-betting profit (you beat the number).
  * Kelly fraction (Thorp / Kelly 1956) — growth-optimal stake; tiny edges imply a
    tiny Kelly and negligible compounding, which is how we filter "minute" edges.
  * Risk-adjusted return (edge / sd, a Sharpe analogue) — an edge swamped by
    variance is not tradeable.
  * Non-parametric bootstrap 95% CI on ROI — robust to the fat-tailed payoff
    distribution; we trust a segment only when its ROI lower bound clears a hurdle.
  * Benjamini-Hochberg false-discovery control — we scan many segments, so we
    correct for multiple comparisons instead of cherry-picking the lucky one.

Only segments that are economically meaningful (ROI LB above the hurdle), robust
(bootstrap CI > 0), AND survive FDR are reported as exploitable vulnerabilities.

    python -m backtest.market_edge [--min-n 30] [--hurdle 0.03] [--bankroll-kelly 0.25]
"""

from __future__ import annotations

import argparse
import math
import random

import db

random.seed(7)


# ── price / staking math ─────────────────────────────────────────────────────
def _roi_unit(prob_entry: float, won: bool) -> float:
    """Profit in units for a 1u bet at fair-decimal = 1/prob_entry (net of stake)."""
    if prob_entry <= 0:
        return 0.0
    dec = 1.0 / prob_entry
    return (dec - 1.0) if won else -1.0


def _kelly(win_rate: float, prob_entry: float) -> float:
    """Growth-optimal fraction at decimal odds b+1 = 1/prob_entry."""
    b = (1.0 / prob_entry) - 1.0 if prob_entry > 0 else 0.0
    if b <= 0:
        return 0.0
    f = (b * win_rate - (1.0 - win_rate)) / b
    return max(0.0, f)


def _bootstrap_roi_ci(rois: list[float], iters: int = 2000, lo: float = 2.5, hi: float = 97.5):
    """Percentile bootstrap CI on mean ROI — robust to the skewed payoff dist."""
    n = len(rois)
    if n == 0:
        return (0.0, 0.0)
    means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += rois[random.randrange(n)]
        means.append(s / n)
    means.sort()
    return (means[int(lo / 100 * iters)], means[min(iters - 1, int(hi / 100 * iters))])


def _benjamini_hochberg(pvals: list[float], q: float = 0.10) -> list[bool]:
    """Return a survival mask under BH false-discovery control at level q."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    survive = [False] * m
    kmax = -1
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= q * rank / m:
            kmax = rank
    for rank, i in enumerate(order, start=1):
        if rank <= kmax:
            survive[i] = True
    return survive


def _normal_sf(z: float) -> float:
    """One-sided p-value P(Z>z) via erfc."""
    return 0.5 * math.erfc(z / math.sqrt(2))


# ── segment scan ─────────────────────────────────────────────────────────────
def _fetch() -> list[dict]:
    return db.select("prediction_market_snapshots",
                     "?settled=eq.true&won=not.is.null&open_prob=not.is.null"
                     "&select=open_prob,implied_probability,delta,won,volume,market_type")


SEGMENTS = [
    ("steam up >=2pt",        lambda r: (r["delta"] or 0) >= 0.02),
    ("steam up >=4pt",        lambda r: (r["delta"] or 0) >= 0.04),
    ("up + liquid >=120k",    lambda r: (r["delta"] or 0) >= 0.02 and (r["volume"] or 0) >= 120000),
    ("up + thin <80k",        lambda r: (r["delta"] or 0) >= 0.02 and (r["volume"] or 0) < 80000),
    ("bet down >=2pt (fade)", lambda r: (r["delta"] or 0) <= -0.02),
    ("down + liquid >=120k",  lambda r: (r["delta"] or 0) <= -0.02 and (r["volume"] or 0) >= 120000),
    ("flat (<1pt)",           lambda r: abs(r["delta"] or 0) < 0.01),
    ("underdog open <0.45",   lambda r: (r["open_prob"] or 1) < 0.45),
    ("favorite open >0.55",   lambda r: (r["open_prob"] or 0) > 0.55),
    ("dog + steam up",        lambda r: (r["open_prob"] or 1) < 0.45 and (r["delta"] or 0) >= 0.02),
]


def scan(min_n: int = 30, hurdle: float = 0.03, kelly_cap: float = 0.25) -> list[dict]:
    rows = [r for r in _fetch() if r.get("market_type") == "ml"]
    if len(rows) < min_n:
        return []

    results = []
    for label, pred in SEGMENTS:
        seg = [r for r in rows if pred(r)]
        n = len(seg)
        if n < min_n:
            results.append({"label": label, "n": n, "enough": False})
            continue
        wins = sum(1 for r in seg if r["won"])
        win_rate = wins / n
        rois = [_roi_unit(r["open_prob"], r["won"]) for r in seg]
        mean_roi = sum(rois) / n
        sd = (sum((x - mean_roi) ** 2 for x in rois) / n) ** 0.5
        se = sd / math.sqrt(n) if n else 0.0
        z = mean_roi / se if se > 0 else 0.0
        p = _normal_sf(z)                       # one-sided: ROI > 0
        lb, ub = _bootstrap_roi_ci(rois)
        avg_entry = sum(r["open_prob"] for r in seg) / n
        clv = sum((r["implied_probability"] - r["open_prob"]) for r in seg) / n
        kelly = min(kelly_cap, _kelly(win_rate, avg_entry))
        results.append({
            "label": label, "n": n, "enough": True, "win_rate": win_rate,
            "roi": mean_roi, "roi_lb": lb, "roi_ub": ub, "sharpe": (mean_roi / sd) if sd else 0.0,
            "clv": clv, "kelly": kelly, "p": p,
        })

    tested = [r for r in results if r.get("enough")]
    survive = _benjamini_hochberg([r["p"] for r in tested], q=0.10) if tested else []
    for r, s in zip(tested, survive):
        r["fdr_ok"] = s
        r["tradeable"] = s and r["roi_lb"] > hurdle and r["kelly"] > 0.01
    return results


def run(min_n: int = 30, hurdle: float = 0.03, kelly_cap: float = 0.25):
    results = scan(min_n, hurdle, kelly_cap)
    if not results:
        print(f"\n  Insufficient settled sample (need >= {min_n}). Run the candlestick backfill.\n")
        return
    print(f"\n  MARKET-EDGE SCAN  (settled ML; n>={min_n}; ROI at ENTRY/open; hurdle {hurdle*100:.0f}% ROI)")
    print(f"  {'SEGMENT':<24}{'N':>5}{'WIN%':>7}{'ROI/u':>8}{'ROI 95%LB':>11}{'CLV':>7}{'KELLY':>7}{'SHRP':>6}  VERDICT")
    for r in results:
        if not r.get("enough"):
            print(f"  {r['label']:<24}{r['n']:>5}   (insufficient sample)")
            continue
        verdict = "** TRADEABLE **" if r["tradeable"] else ("survives-FDR" if r["fdr_ok"] else "noise")
        print(f"  {r['label']:<24}{r['n']:>5}{r['win_rate']*100:>6.1f}%{r['roi']*100:>+7.1f}"
              f"{r['roi_lb']*100:>+10.1f}{r['clv']*100:>+6.1f}{r['kelly']*100:>6.1f}%{r['sharpe']:>6.2f}  {verdict}")

    trade = [r for r in results if r.get("tradeable")]
    print()
    if trade:
        print("  EXPLOITABLE vulnerabilities (profit-positive, robust, FDR-controlled):")
        for r in sorted(trade, key=lambda x: -x["kelly"]):
            print(f"   - {r['label']}: +{r['roi']*100:.1f}% ROI/u (95% LB +{r['roi_lb']*100:.1f}%), "
                  f"Kelly {r['kelly']*100:.1f}%, CLV {r['clv']*100:+.1f}, n={r['n']}")
    else:
        print("  No segment clears the profit hurdle + robustness + FDR yet. The closing")
        print("  line is near-efficient; meaningful edge needs the larger sample + forward")
        print("  CLV capture. We report nothing rather than chase minute (losing) edges.")
    print("\n  ROI is per unit at the entry (open) price. CLV>0 means the line moved your")
    print("  way after entry. KELLY is growth-optimal stake (capped). SHRP = ROI/sd.\n")


def main():
    p = argparse.ArgumentParser(description="Profit-focused market-edge scan.")
    p.add_argument("--min-n", type=int, default=30)
    p.add_argument("--hurdle", type=float, default=0.03, help="Min ROI lower bound to call tradeable")
    p.add_argument("--kelly-cap", type=float, default=0.25)
    run(p.parse_args().min_n, p.parse_args().hurdle, p.parse_args().kelly_cap)


if __name__ == "__main__":
    main()
