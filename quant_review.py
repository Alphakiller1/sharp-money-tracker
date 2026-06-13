"""
Portfolio-level quant review for the sharp tracker's settled record.

`market_edge.py` already scores individual SEGMENTS (bootstrap CI + FDR + Kelly +
CLV per segment). This is the complementary *portfolio* view:

  1. Market efficiency — is the de-vigged price itself calibrated? (Brier / Murphy)
  2. Follow-the-steam portfolio — ROI bootstrap CI + bootstrap p-value
  3. Wilson 95% win-rate interval (small-sample-safe)
  4. CLV beat-rate (did the line keep moving our way after entry?)
  5. Fractional-Kelly growth simulation (full / half / quarter)
  6. SPRT sequential monitor — is the steam edge real yet?

Reuses the statistical helpers in market_edge so there is one implementation.
Theory: vault 06-Betting-Logic/Quant-Theory-Foundations.md.

    python quant_review.py                 # console + vault report
    python quant_review.py --no-write
    python quant_review.py --steam 0.02     # steam threshold (toward-side prob move)
"""

from __future__ import annotations

import argparse
import math
from datetime import date, datetime

import config
import db
from market_edge import _roi_unit, _bootstrap_roi_ci, _kelly, _benjamini_hochberg

Z95 = 1.959963985


def _wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = (z / d) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, centre - half), min(1.0, centre + half))


def _required_n(p: float = 0.5, h: float = 0.05, z: float = Z95) -> int:
    return math.ceil(z * z * p * (1 - p) / (h * h))


def _brier_murphy(pairs: list[tuple[float, int]], bins: int = 10) -> dict:
    n = len(pairs)
    if n == 0:
        return {}
    bs = sum((p - o) ** 2 for p, o in pairs) / n
    base = sum(o for _, o in pairs) / n
    rel = res = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        grp = [(p, o) for p, o in pairs if (lo <= p < hi or (b == bins - 1 and p == 1.0))]
        if not grp:
            continue
        nk = len(grp)
        pbar = sum(p for p, _ in grp) / nk
        obar = sum(o for _, o in grp) / nk
        rel += nk * (pbar - obar) ** 2
        res += nk * (obar - base) ** 2
    return {"brier": bs, "reliability": rel / n, "resolution": res / n,
            "uncertainty": base * (1 - base), "base": base, "n": n}


def _bootstrap_p(rois: list[float], iters: int = 10000) -> float:
    import random
    random.seed(7)
    n = len(rois)
    if n == 0:
        return 1.0
    point = sum(rois) / n
    centred = [x - point for x in rois]
    obs = abs(point)
    extreme = 0
    for _ in range(iters):
        s = sum(centred[random.randrange(n)] for _ in range(n))
        if abs(s / n) >= obs:
            extreme += 1
    return extreme / iters


def _kelly_growth(recs: list[tuple[float, float, int]], fraction: float) -> dict:
    bk = peak = 1.0
    dd = 0.0
    staked = 0
    for p, entry, won in recs:
        dec = 1.0 / entry if entry and entry > 0 else 0
        b = dec - 1
        if b <= 0:
            continue
        edge = (b * p - (1 - p)) / b
        f = max(0.0, edge) * fraction
        if f <= 0:
            continue
        staked += 1
        stake = bk * f
        bk += stake * b if won else -stake
        peak = max(peak, bk)
        dd = max(dd, (peak - bk) / peak if peak > 0 else 0.0)
    return {"final": bk, "dd": dd, "staked": staked, "growth": (bk - 1) * 100}


def _sprt(k: int, n: int, p0: float, p1: float, alpha=0.05, beta=0.10) -> dict:
    if not (0 < p0 < 1 and 0 < p1 < 1) or n == 0:
        return {}
    llr = k * math.log(p1 / p0) + (n - k) * math.log((1 - p1) / (1 - p0))
    upper = math.log((1 - beta) / alpha)
    lower = math.log(beta / (1 - alpha))
    dec = ("ACCEPT H1 (edge confirmed)" if llr >= upper
           else "ACCEPT H0 (no edge — stop)" if llr <= lower
           else "CONTINUE (keep sampling)")
    return {"llr": llr, "upper": upper, "lower": lower, "decision": dec}


def _fetch() -> list[dict]:
    try:
        return db.select(
            "prediction_market_snapshots",
            "?settled=eq.true&won=not.is.null&open_prob=not.is.null"
            "&market_type=eq.ml&select=open_prob,implied_probability,delta,won")
    except SystemExit as exc:
        print(f"  Supabase not configured ({str(exc).splitlines()[0]}).")
        return []
    except Exception as exc:
        print(f"  Fetch failed: {type(exc).__name__}: {exc}")
        return []


def build_report(rows: list[dict], steam: float) -> str:
    L: list[str] = []
    today = date.today().isoformat()
    L.append(f"# Sharp Quant Review ({today})")
    L.append("")
    L.append("Portfolio battery over settled ML snapshots — complements the per-segment "
             "scan in `market_edge.py`. Theory: [[../06-Betting-Logic/Quant-Theory-Foundations]].")
    L.append("")
    n_all = len(rows)
    L.append(f"## 1. Market efficiency — is the price calibrated?")
    L.append(f"- Settled ML snapshots: **{n_all}**  (need ≥ {_required_n()} per "
             f"calibration bucket for ±5pts)")
    if n_all == 0:
        L.append("\n> No settled snapshots. Run the tracker + settlement first.")
        return "\n".join(L)

    cal = [(float(r["implied_probability"]), 1 if r["won"] else 0)
           for r in rows if r.get("implied_probability") is not None]
    bm = _brier_murphy(cal)
    L.append(f"- Closing-price Brier: **{bm['brier']:.4f}** | reliability "
             f"**{bm['reliability']:.4f}** (≈0 ⇒ the close is efficient) | "
             f"resolution {bm['resolution']:.4f}")
    L.append("- `implied_probability` is the **close**; `open_prob` the open; "
             "`delta = close − open`. A near-zero reliability is expected — the close "
             "is honest, so any edge must come from entering at the *open* before the move.")

    # PRIMARY (executable): back every logged sharp-favored side at the OPEN price,
    # across the full settled set. This is the product thesis and is decided at entry —
    # unlike a delta cut, which selects on the future move (hindsight, not tradeable).
    full = [r for r in rows if r.get("open_prob") is not None]
    nf = len(full)
    wins = sum(1 for r in full if r["won"])
    rois = [_roi_unit(r["open_prob"], r["won"]) for r in full]
    L.append("")
    L.append("## 2. Executable portfolio — back the tracked side at the open")
    L.append(f"- Bets (all settled, entry at open): **{nf}**")
    if nf >= 20:
        mean = sum(rois) / nf
        lb, ub = _bootstrap_roi_ci(rois)
        pval = _bootstrap_p(rois)
        wl, wh = _wilson(wins, nf)
        L.append(f"- Record: {wins}–{nf - wins} ({wins/nf:.1%}; Wilson 95% {wl:.1%}–{wh:.1%})")
        L.append(f"- ROI/unit at open: **{mean*100:+.2f}%** (bootstrap 95% CI "
                 f"{lb*100:+.2f}% … {ub*100:+.2f}%)")
        L.append(f"- Bootstrap p vs break-even: **{pval:.3f}** "
                 f"({'significant' if pval < 0.05 else 'not significant'} at α=0.05)")
        # CLV across the FULL set = mean delta (close − open). Non-circular here because
        # the set is NOT selected on delta — this is the genuine timing/skill signal.
        clv = sum((r["implied_probability"] - r["open_prob"]) for r in full) / nf
        beat = sum(1 for r in full if (r["implied_probability"] - r["open_prob"]) > 0)
        cl, ch = _wilson(beat, nf)
        L.append("")
        L.append("## 3. CLV across the full set (the real skill signal)")
        L.append(f"- Entries that beat the close: **{beat}/{nf} = {beat/nf:.1%}** "
                 f"(Wilson {cl:.1%}–{ch:.1%})")
        L.append(f"- Mean CLV (close − open): **{clv*100:+.2f}** implied-prob points")
        L.append("- CLV converges on skill far faster than ROI; sustained positive CLV "
                 "across the full (unselected) set is the load-bearing evidence.")
        L.append("")
        L.append("## 4. Edge-aware Kelly sizing (uncertainty-respecting)")
        # Honest sizing: edge per unit = mean ROI; its bootstrap CI is the uncertainty.
        # If the lower CI bound is <= 0 the edge isn't established, so prudent stake = 0.
        if lb > 0:
            kf = max(0.0, mean)  # mean ROI ≈ Kelly edge fraction at fair odds
            L.append(f"- Measured edge **{mean*100:+.2f}%** with 95% lower bound "
                     f"**{lb*100:+.2f}%** > 0 → quarter-Kelly stake ≈ **{kf*25:.2f}%** of "
                     f"bankroll per bet (¼ of edge).")
        else:
            L.append(f"- Measured edge **{mean*100:+.2f}%** but 95% lower bound "
                     f"**{lb*100:+.2f}%** ≤ 0 → **prudent stake = 0**. Kelly on an "
                     f"unproven edge over-bets noise; size only once the CI clears zero.")
        L.append("- (A bankroll-growth sim using the *closing* prob as truth would read "
                 "thousands-of-percent, but that injects look-ahead — the close isn't "
                 "known at entry — so it is omitted by design.)")
        L.append("")
        L.append("## 5. SPRT sequential monitor — is the edge real yet?")
        s = _sprt(wins, nf, 0.50, 0.524)
        if s:
            L.append(f"- H0: win rate 0.500 vs H1: 0.524 (≈ −110 break-even).")
            L.append(f"- LLR **{s['llr']:+.3f}** (accept-edge ≥ {s['upper']:.2f}, "
                     f"no-edge ≤ {s['lower']:.2f}) → **{s['decision']}**")
    else:
        L.append(f"- ⚠ Only {nf} settled bets — need ≥ {_required_n(0.5, 0.05)} for a "
                 f"tight win-rate CI. Sections 3–5 deferred.")

    # DESCRIPTIVE ONLY: the steam cut selects on the future move, so it is NOT an
    # executable ROI — shown purely to characterise what steamed sides did.
    steamed = [r for r in rows if (r.get("delta") or 0) >= steam]
    ns = len(steamed)
    L.append("")
    L.append(f"## 6. Steam cut (Δ ≥ {steam:+.2f}) — *descriptive, hindsight, not tradeable*")
    if ns:
        sw = sum(1 for r in steamed if r["won"])
        L.append(f"- {ns} sides closed ≥{steam:.0%} up from open; they won {sw}/{ns} "
                 f"({sw/ns:.1%}). Selecting on Δ is look-ahead — use this only to "
                 f"understand the population, never as a backtest ROI.")
    else:
        L.append("- None this sample.")
    L.append("")
    L.append(f"_Generated {datetime.now().isoformat(timespec='seconds')}._")
    return "\n".join(L)


def _safe_print(text: str) -> None:
    import sys
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(enc, "replace").decode(enc))


def run(write: bool = True, steam: float = 0.02) -> None:
    rows = _fetch()
    report = build_report(rows, steam)
    print()
    _safe_print(report)
    if write and rows:
        try:
            out = config.VAULT_ROOT / "15-Reports"
            out.mkdir(parents=True, exist_ok=True)
            path = out / f"Sharp-Quant-Review-{date.today().isoformat()}.md"
            path.write_text(report, encoding="utf-8")
            print(f"\n  Wrote {path}")
        except OSError as exc:
            print(f"\n  (vault write failed: {exc})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Portfolio quant review of the settled sharp record.")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--steam", type=float, default=0.02, help="Steam threshold (default 0.02).")
    args = ap.parse_args()
    run(write=not args.no_write, steam=args.steam)


if __name__ == "__main__":
    main()
