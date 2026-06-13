"""
Market data scraper -- pulls betting odds the vault and mlbma_pipeline don't have.

Source: The Odds API (the-odds-api.com), free tier. Returns DraftKings + all major
US books, so we get line movement, best-price shopping, and closing lines for CLV.

What it pulls:
  - Game markets (1 cheap request for the whole slate): moneyline, run line, totals.
  - Player props / team totals (per-event requests, more credits): via --props.
  - A timestamped snapshot every run -> odds_history.csv (line movement + closing line)
    plus odds_latest.csv (newest snapshot the evaluator reads).

Setup:
  1. Get a free key at https://the-odds-api.com  (no cost on the free tier).
  2. Set it:  $env:ODDS_API_KEY = "your_key"   (PowerShell)
     or paste into config.ODDS_API_KEY_FALLBACK.

Usage:
  python market_data.py --fetch              # pull game odds, snapshot the slate
  python market_data.py --fetch --props      # also pull props/team totals (costs more)
  python market_data.py --show "ARI@SEA"     # show current odds + movement for a game
  python market_data.py --usage              # remaining API quota from last response
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

import pandas as pd

import config
from config import (
    EVAL_DATA_DIR,
    ODDS_API_BASE,
    ODDS_API_KEY,
    ODDS_API_MIN_REMAINING,
    ODDS_BOOKMAKERS,
    ODDS_FORMAT,
    ODDS_GAME_MARKETS,
    ODDS_HISTORY_CSV,
    ODDS_LATEST_CSV,
    ODDS_PROP_MARKETS,
    ODDS_REGIONS,
    ODDS_SPORT_KEY,
    PIPELINE_DATA_DIR,
    team_abbr,
)

# Normalized snapshot columns.
COLUMNS = [
    "fetched_at", "commence_time", "event_id", "away", "home",
    "book", "market", "side", "line", "odds",
]

_LAST_USAGE: dict[str, str] = {}
# Quota cache persisted across runs so check_quota() works pre-fetch.
_USAGE_FILE = EVAL_DATA_DIR / "odds_api_usage.json"


# ── HTTP ─────────────────────────────────────────────────────────────────────


def _get(path: str, params: dict[str, Any]) -> Any:
    if not ODDS_API_KEY:
        raise SystemExit(
            "No ODDS_API_KEY set. Get a free key at https://the-odds-api.com, then\n"
            '  PowerShell:  $env:ODDS_API_KEY = "your_key"\n'
            "  or paste it into config.py (ODDS_API_KEY).")
    params = {**params, "apiKey": ODDS_API_KEY}
    url = f"{ODDS_API_BASE}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            _LAST_USAGE["remaining"] = resp.headers.get("x-requests-remaining", "?")
            _LAST_USAGE["used"] = resp.headers.get("x-requests-used", "?")
            try:
                EVAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
                _USAGE_FILE.write_text(json.dumps({
                    **_LAST_USAGE,
                    "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }))
            except OSError:
                pass
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        raise SystemExit(f"Odds API error {e.code}: {body}") from e


def check_quota() -> None:
    """Abort a paid fetch when the last-known remaining quota is below the floor.
    No-op when no usage has ever been recorded (first run) or floor <= 0."""
    floor = ODDS_API_MIN_REMAINING
    rem = _LAST_USAGE.get("remaining")
    if rem in (None, "?") and _USAGE_FILE.exists():
        try:
            rem = json.loads(_USAGE_FILE.read_text()).get("remaining")
        except (OSError, ValueError):
            return
    try:
        rem = float(rem)
    except (TypeError, ValueError):
        return
    if floor > 0 and rem < floor:
        raise SystemExit(
            f"  Odds API quota low: {rem:.0f} requests remaining (< floor {floor}).\n"
            "  Skipping fetch. Override with ODDS_API_MIN_REMAINING=0.")


def print_usage() -> None:
    """One-line API quota readout (in-process, falling back to the cached file)."""
    u: dict = _LAST_USAGE
    cached = False
    if not u and _USAGE_FILE.exists():
        try:
            u = json.loads(_USAGE_FILE.read_text())
            cached = True
        except (OSError, ValueError):
            u = {}
    if u:
        note = f" (cached {u.get('checked_at', '')})" if cached else ""
        print(f"  API quota: used {u.get('used')}, remaining {u.get('remaining')}{note}.")


# ── Normalization ────────────────────────────────────────────────────────────


def _normalize_event(ev: dict, fetched_at: str) -> list[dict]:
    """Flatten one Odds API event (all books, all game markets) into rows."""
    away = team_abbr(ev.get("away_team", ""))
    home = team_abbr(ev.get("home_team", ""))
    base = {
        "fetched_at": fetched_at,
        "commence_time": ev.get("commence_time", ""),
        "event_id": ev.get("id", ""),
        "away": away,
        "home": home,
    }
    rows: list[dict] = []
    for bk in ev.get("bookmakers", []):
        book = bk.get("key", "")
        for mk in bk.get("markets", []):
            mkey = mk.get("key", "")
            for oc in mk.get("outcomes", []):
                name = oc.get("name", "")
                point = oc.get("point", "")
                price = oc.get("price", "")
                if mkey == "h2h":
                    market, side, line = "ml", team_abbr(name), ""
                elif mkey == "spreads":
                    market, side, line = "runline", team_abbr(name), point
                elif mkey == "totals":
                    market, side, line = "total", str(name).lower(), point
                elif mkey == "team_totals":
                    # name like "Over"/"Under"; description carries team on some books
                    market = "team_total"
                    side = f"{team_abbr(oc.get('description', home))}_{str(name).lower()}"
                    line = point
                else:
                    # player props: keep raw market key; side = player + over/under
                    market = mkey
                    side = f"{oc.get('description', name)}|{str(name).lower()}"
                    line = point
                rows.append({**base, "book": book, "market": market,
                             "side": side, "line": line, "odds": price})
    return rows


# ── Fetch + store ─────────────────────────────────────────────────────────────


def fetch_game_odds() -> list[dict]:
    params = {"regions": ODDS_REGIONS, "markets": ODDS_GAME_MARKETS,
              "oddsFormat": ODDS_FORMAT}
    if ODDS_BOOKMAKERS:
        params["bookmakers"] = ODDS_BOOKMAKERS
    data = _get(f"/sports/{ODDS_SPORT_KEY}/odds", params)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    for ev in data:
        rows.extend(_normalize_event(ev, fetched_at))
    return rows


def fetch_prop_odds(event_ids: list[str]) -> list[dict]:
    """Per-event props/team-totals. Costs more credits; best-effort per event."""
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    for eid in event_ids:
        params = {"regions": ODDS_REGIONS, "markets": ODDS_PROP_MARKETS,
                  "oddsFormat": ODDS_FORMAT}
        if ODDS_BOOKMAKERS:
            params["bookmakers"] = ODDS_BOOKMAKERS
        try:
            ev = _get(f"/sports/{ODDS_SPORT_KEY}/events/{eid}/odds", params)
            rows.extend(_normalize_event(ev, fetched_at))
        except SystemExit as e:
            print(f"  prop fetch skipped for {eid}: {e}")
    return rows


def store(rows: list[dict]) -> None:
    """Append every fetch to history (movement record) and merge into the latest
    cache, replacing only the games that were just fetched."""
    if not rows:
        print("  No odds rows to store.")
        return
    EVAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=COLUMNS)

    # Append to history (line-movement record; one snapshot per fetch).
    if ODDS_HISTORY_CSV.exists():
        df.to_csv(ODDS_HISTORY_CSV, mode="a", header=False, index=False)
    else:
        df.to_csv(ODDS_HISTORY_CSV, index=False)

    # Merge into latest: drop prior rows for the fetched games, keep the rest.
    fetched_games = set(zip(df["away"], df["home"]))
    if ODDS_LATEST_CSV.exists():
        prev = pd.read_csv(ODDS_LATEST_CSV, dtype=str).fillna("")
        keep = prev[~prev.apply(lambda r: (r["away"], r["home"]) in fetched_games, axis=1)]
        merged = pd.concat([keep, df.astype(str)], ignore_index=True)
    else:
        merged = df.astype(str)
    merged.to_csv(ODDS_LATEST_CSV, index=False)

    games = df.groupby(["away", "home"]).ngroups
    print(f"  Stored {len(df)} odds rows across {games} game(s) "
          f"({df['book'].nunique()} books).")


# ── On-demand per-game fetch (the default path: fetch when a bet is put in) ────


def list_events() -> dict[tuple[str, str], str]:
    """Map (away, home) -> event_id for the current slate. Free (0 credits)."""
    out: dict[tuple[str, str], str] = {}
    for ev in upcoming_slate():
        out[(ev["away"], ev["home"])] = ev["event_id"]
    return out


def upcoming_slate() -> list[dict]:
    """Upcoming MLB games from the live board (commence times). Free (0 credits)."""
    data = _get(f"/sports/{ODDS_SPORT_KEY}/events", {})
    games = []
    for ev in data:
        games.append({
            "away": team_abbr(ev.get("away_team", "")),
            "home": team_abbr(ev.get("home_team", "")),
            "event_id": ev.get("id", ""),
            "commence_time": ev.get("commence_time", ""),
        })
    return sorted(games, key=lambda g: g["commence_time"])


def _pipeline_games() -> set[tuple[str, str]]:
    """Games the pipeline has metrics for (today_matchups.csv) -> evaluable set."""
    import os as _os
    path = _os.path.join(PIPELINE_DATA_DIR, "today_matchups.csv")
    if not _os.path.exists(path):
        return set()
    m = pd.read_csv(path)
    return set(zip(m["Away"].astype(str).str.upper().str.strip(),
                   m["Home"].astype(str).str.upper().str.strip()))


def show_slate() -> None:
    games = upcoming_slate()
    evaluable = _pipeline_games()
    print(f"\n  Upcoming MLB games (live board) - {len(games)} games")
    print(f"  {'GAME':<12} {'START (UTC)':<22} EVALUABLE?")
    for g in games:
        tag = "yes" if (g["away"], g["home"]) in evaluable else "no metrics"
        print(f"  {g['away']+'@'+g['home']:<12} {g['commence_time']:<22} {tag}")
    if _LAST_USAGE:
        print(f"  (events list is free; quota remaining {_LAST_USAGE.get('remaining')})")
    print()


def fetch_event_odds(event_id: str, props: bool = False) -> list[dict]:
    markets = ODDS_GAME_MARKETS + ("," + ODDS_PROP_MARKETS if props else "")
    params = {"regions": ODDS_REGIONS, "markets": markets, "oddsFormat": ODDS_FORMAT}
    if ODDS_BOOKMAKERS:
        params["bookmakers"] = ODDS_BOOKMAKERS
    ev = _get(f"/sports/{ODDS_SPORT_KEY}/events/{event_id}/odds", params)
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return _normalize_event(ev, fetched_at)


def fetch_historical(date_iso: str, props: bool = False) -> list[dict]:
    """Past odds at a point in time. PAID-ONLY on The Odds API (free plan returns
    401). Ready for when a paid key is set; degrades gracefully otherwise.
    date_iso e.g. '2026-05-15T18:00:00Z'."""
    markets = ODDS_GAME_MARKETS + ("," + ODDS_PROP_MARKETS if props else "")
    params = {"regions": ODDS_REGIONS, "markets": markets,
              "oddsFormat": ODDS_FORMAT, "date": date_iso}
    try:
        payload = _get(f"/historical/sports/{ODDS_SPORT_KEY}/odds", params)
    except SystemExit as e:
        if "FREE_USAGE_PLAN" in str(e) or "401" in str(e):
            print("  Historical odds are paid-only on The Odds API — skipping past-odds "
                  "scrape. Outcomes still backfill for free (backtest.import_history).")
            return []
        raise
    # historical response wraps events under 'data'
    events = payload.get("data", payload) if isinstance(payload, dict) else payload
    fetched = date_iso
    rows = []
    for ev in events:
        rows.extend(_normalize_event(ev, fetched))
    return rows


def fetch_game(away: str, home: str, props: bool = False) -> list[dict]:
    """Live fetch + store odds for one game. Returns its rows (may be empty)."""
    check_quota()
    away, home = away.upper(), home.upper()
    events = list_events()
    eid = events.get((away, home))
    if not eid:
        print(f"  {away}@{home} not on the live board (started, finished, or not posted).")
        return []
    rows = fetch_event_odds(eid, props=props)
    store(rows)
    print_usage()
    return rows


def run_fetch(props: bool = False) -> None:
    check_quota()
    print("Fetching game odds from The Odds API...")
    rows = fetch_game_odds()
    if props:
        ids = sorted({r["event_id"] for r in rows if r["event_id"]})
        print(f"Fetching props/team-totals for {len(ids)} events...")
        rows.extend(fetch_prop_odds(ids))
    store(rows)
    print_usage()


# ── Lookups (used by the evaluator) ───────────────────────────────────────────


def _load_latest() -> pd.DataFrame | None:
    if not ODDS_LATEST_CSV.exists():
        return None
    df = pd.read_csv(ODDS_LATEST_CSV, dtype=str).fillna("")
    return df


def _match(df: pd.DataFrame, away: str, home: str, market: str,
           side: str, line: float | None) -> pd.DataFrame:
    m = (df["away"] == away) & (df["home"] == home) & (df["market"] == market)
    m &= df["side"].str.upper() == side.upper()
    if line is not None and market != "ml":
        m &= df["line"].apply(lambda x: _eqline(x, line))
    return df[m]


def _eqline(x: str, line: float) -> bool:
    try:
        return abs(float(x) - float(line)) < 1e-6
    except (TypeError, ValueError):
        return False


def best_price(away: str, home: str, market: str, side: str,
               line: float | None = None) -> dict | None:
    """Best available American price across books for a bet (from latest snapshot)."""
    df = _load_latest()
    if df is None:
        return None
    rows = _match(df, away, home, market, side, line)
    if rows.empty:
        return None
    rows = rows.assign(odds_num=pd.to_numeric(rows["odds"], errors="coerce")).dropna(subset=["odds_num"])
    if rows.empty:
        return None
    best = rows.loc[rows["odds_num"].idxmax()]   # highest American = best for bettor
    return {"odds": int(best["odds_num"]), "book": best["book"], "line": best["line"],
            "n_books": int(rows["book"].nunique())}


def line_movement(away: str, home: str, market: str, side: str,
                  line: float | None = None) -> dict | None:
    """Consensus open vs current from history (median across books per snapshot)."""
    if not ODDS_HISTORY_CSV.exists():
        return None
    df = pd.read_csv(ODDS_HISTORY_CSV, dtype=str).fillna("")
    rows = _match(df, away, home, market, side, line)
    if rows.empty:
        return None
    rows = rows.assign(odds_num=pd.to_numeric(rows["odds"], errors="coerce")).dropna(subset=["odds_num"])
    if rows.empty:
        return None
    by_snap = rows.groupby("fetched_at")["odds_num"].median().sort_index()
    if len(by_snap) < 1:
        return None
    return {"open": int(by_snap.iloc[0]), "current": int(by_snap.iloc[-1]),
            "delta": int(by_snap.iloc[-1] - by_snap.iloc[0]), "snapshots": len(by_snap)}


# ── CLI ────────────────────────────────────────────────────────────────────────


def show_game(game: str) -> None:
    away, home = (s.strip().upper() for s in game.split("@", 1))
    df = _load_latest()
    if df is None:
        raise SystemExit("No odds snapshot yet. Run: python market_data.py --fetch")
    g = df[(df["away"] == away) & (df["home"] == home)]
    if g.empty:
        raise SystemExit(f"{away}@{home} not in latest snapshot.")
    print(f"\n  {away} @ {home} - current market ({g['book'].nunique()} books)")
    for market in ["ml", "runline", "total", "team_total"]:
        sub = g[g["market"] == market]
        if sub.empty:
            continue
        print(f"  {market.upper()}:")
        for side in sorted(sub["side"].unique()):
            ss = sub[sub["side"] == side].assign(o=pd.to_numeric(sub["odds"], errors="coerce"))
            row = ss.loc[ss["o"].idxmax()]
            mv = line_movement(away, home, market, side,
                               float(row["line"]) if row["line"] else None)
            mv_s = f"  (open {mv['open']:+d} -> now {mv['current']:+d}, d{mv['delta']:+d})" if mv else ""
            line_s = f" {row['line']}" if row["line"] else ""
            print(f"    {side}{line_s}: best {int(row['o']):+d} @ {row['book']}{mv_s}")
    print()


def main() -> None:
    p = argparse.ArgumentParser(description="Scrape MLB betting market data.")
    p.add_argument("--slate", action="store_true", help="List upcoming games (free, 0 credits)")
    p.add_argument("--fetch", action="store_true", help="Fetch the whole slate (scan)")
    p.add_argument("--fetch-game", metavar="GAME", help='Fetch one game on demand, "AWAY@HOME"')
    p.add_argument("--props", action="store_true", help="Also fetch props/team totals")
    p.add_argument("--show", metavar="GAME", help='Show market for "AWAY@HOME"')
    p.add_argument("--usage", action="store_true", help="Show API quota note")
    args = p.parse_args()

    if args.slate:
        show_slate()
    if args.fetch:
        run_fetch(props=args.props)
    if args.fetch_game:
        away, home = (s.strip() for s in args.fetch_game.split("@", 1))
        print(f"Fetching live odds for {away.upper()}@{home.upper()}...")
        fetch_game(away, home, props=args.props)
    if args.show:
        show_game(args.show)
    if args.usage:
        print_usage()
        if not _LAST_USAGE and not _USAGE_FILE.exists():
            print("  No API usage recorded yet (run --slate or --fetch first).")
    if not (args.slate or args.fetch or args.fetch_game or args.show or args.usage):
        p.print_help()


if __name__ == "__main__":
    main()
