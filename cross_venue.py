"""
Cross-venue edge engine — treat each Kalshi contract as a binary option and price
it against the sportsbook market (and vice-versa). Three signals:

  1. ARBITRAGE — bet side A on the cheaper venue (commercial consensus) and side B on the other; if the two
     costs sum to < 1 the profit is locked regardless of outcome (Dutch book).
  2. VALUE     — one venue prices a side cheaper than the other venue's no-vig fair;
     positive EV to take the cheap side (the other venue is the reference).
  3. THIN-EDGE — a Kalshi contract whose price diverges from the sportsbook consensus
     AND has low liquidity: thin books misprice, and you can take them.

A contract is just a binary option with payoff 1 if it resolves yes; cost = price.
EV(buy yes) = fair*1 - price; arb is a costless straddle across venues.

    python -m backtest.cross_venue [--thin 80000] [--min-edge 0.02]
"""

from __future__ import annotations

import argparse

import db

VIG_MARGIN = 0.0      # require strictly < 1 for arb; raise to demand a cushion
MAX_PLAUSIBLE = 0.12  # cross-venue gaps above this are almost always a stale/
                      # mismatched line, not real edge — flagged "verify", not actioned.

# Commercial mainstream books only — we reference the consensus a normal bettor can
# actually get, not best-of-30 across obscure offshore/exchange books (which created
# phantom arbs). Line-MOVEMENT analysis still uses every book; pricing uses these.
COMMERCIAL_BOOKS = {
    "draftkings", "fanduel", "betmgm", "caesars", "williamhill_us", "betrivers",
    "espnbet", "fanatics", "pointsbetus", "bovada", "betonlineag", "betus",
    "mybookieag", "lowvig", "hardrockbet", "ballybet", "fliff", "pinnacle",
}


def _commercial_ml() -> dict:
    """Commercial-book consensus (median) implied prob per (game_pk, selection)."""
    import statistics
    rows = db.select("odds_snapshots",
                     "?market_type=eq.ml&select=game_pk,selection,implied_probability,sportsbook,snapshot_time"
                     "&order=snapshot_time.desc&limit=6000")
    seen, by_sel = set(), {}
    for r in rows:
        ip = r.get("implied_probability")
        bk = r.get("sportsbook")
        if ip is None or bk not in COMMERCIAL_BOOKS:
            continue
        k = (r["game_pk"], r["selection"], bk)        # one latest price per book
        if k in seen:
            continue
        seen.add(k)
        by_sel.setdefault((r["game_pk"], r["selection"]), []).append(ip)
    return {key: {"implied": round(statistics.median(v), 4), "book": f"{len(v)} commercial"}
            for key, v in by_sel.items()}


def _kalshi_ml() -> dict:
    rows = db.select("v_pm_latest", "?market_type=eq.ml&select=game_pk,selection,implied_probability,volume")
    return {(r["game_pk"], r["selection"]): r for r in rows}


def compute(thin: float = 80000, min_edge: float = 0.02):
    books = _commercial_ml()
    kalshi = _kalshi_ml()
    # group selections per game
    games: dict = {}
    for (gpk, sel) in set(list(books) + list(kalshi)):
        games.setdefault(gpk, set()).add(sel)

    arbs, values, thins = [], [], []
    for gpk, sels in games.items():
        sels = sorted(sels)
        for sel in sels:
            k = kalshi.get((gpk, sel))
            b = books.get((gpk, sel))
            if not k or not b:
                continue
            kp, bp = k["implied_probability"], b["implied"]
            vol = k.get("volume") or 0
            gap = abs(kp - bp)
            suspect = gap > MAX_PLAUSIBLE
            # VALUE: take the side on whichever venue is cheaper vs the other's price.
            if bp - kp >= min_edge:
                values.append((gpk, sel, "kalshi", round(bp - kp, 4), kp, bp, vol, suspect))
            elif kp - bp >= min_edge:
                values.append((gpk, sel, "book", round(kp - bp, 4), bp, kp, vol, suspect))
            # THIN-EDGE: divergent + low-liquidity Kalshi contract (plausible gaps only).
            if min_edge <= gap <= MAX_PLAUSIBLE and vol < thin:
                thins.append((gpk, sel, round(kp - bp, 4), vol))
        # ARBITRAGE: opposite sides across venues.
        if len(sels) == 2:
            a, bb = sels
            ka, kb = kalshi.get((gpk, a)), kalshi.get((gpk, bb))
            ba, bbk = books.get((gpk, a)), books.get((gpk, bb))
            if ka and bbk:   # buy A on Kalshi, B on book
                cost = ka["implied_probability"] + bbk["implied"]
                if cost < 1 - VIG_MARGIN:
                    arbs.append((gpk, f"{a}@kalshi + {bb}@{bbk['book']}", round(cost, 4), round((1 - cost) * 100, 2)))
            if kb and ba:    # buy B on Kalshi, A on book
                cost = kb["implied_probability"] + ba["implied"]
                if cost < 1 - VIG_MARGIN:
                    arbs.append((gpk, f"{bb}@kalshi + {a}@{ba['book']}", round(cost, 4), round((1 - cost) * 100, 2)))

    return games, arbs, values, thins


def run(thin: float = 80000, min_edge: float = 0.02):
    games, arbs, values, thins = compute(thin, min_edge)
    print(f"\n  CROSS-VENUE EDGE  (Kalshi vs sportsbooks; {len(games)} games; min edge {min_edge*100:.0f}%)")
    real_arbs = [a for a in arbs if a[3] <= MAX_PLAUSIBLE * 100]
    print(f"\n  ARBITRAGE (locked profit; verify limits + simultaneity before trusting):")
    for g, legs, cost, profit in sorted(arbs, key=lambda x: x[2]):
        tag = "" if profit <= MAX_PLAUSIBLE * 100 else "   [!] verify (large=likely stale book)"
        print(f"   - {legs}: cost {cost} -> +{profit}% locked{tag}")
    if not arbs:
        print("   (none right now — efficient cross-venue pricing)")

    print(f"\n  VALUE (cheap side vs the other venue's price): {len(values)}")
    for g, sel, venue, edge, take, ref, vol, suspect in sorted(values, key=lambda x: -x[3])[:15]:
        tag = "  [!] verify (gap too big)" if suspect else ""
        print(f"   - {sel}: take on {venue} @ {take:.3f} vs {ref:.3f} ref  -> +{edge*100:.1f}% value (vol {vol:,.0f}){tag}")

    print(f"\n  THIN-EDGE (divergent + low-liquidity Kalshi <{thin:,.0f} vol): {len(thins)}")
    for g, sel, div, vol in sorted(thins, key=lambda x: abs(x[2]), reverse=True)[:15]:
        print(f"   - {sel}: Kalshi {div*100:+.1f} pts vs books, vol {vol:,.0f}  (thin -> exploitable)")
    print("\n  Contracts priced as binary options; value = fair(other venue) - cost.")
    print("  Arb = costless straddle. Thin markets misprice; take the divergent cheap side.\n")


def main():
    p = argparse.ArgumentParser(description="Cross-venue arbitrage / value / thin-edge engine.")
    p.add_argument("--thin", type=float, default=80000, help="Kalshi volume below this = thin")
    p.add_argument("--min-edge", type=float, default=0.02)
    run(p.parse_args().thin, p.parse_args().min_edge)


if __name__ == "__main__":
    main()
