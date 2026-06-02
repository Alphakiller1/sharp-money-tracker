"""
Prediction-market ingestion — Kalshi (free, public API) MLB game-winner prices.

Kalshi is a regulated real-money exchange; its no-vig contract prices are an
independent reference to cross-check against sportsbooks and our model. We log a
timestamped snapshot per run, so movement accumulates (open -> close) the same way
sportsbook odds do — and the closing price gives a second CLV anchor.

  python -m backtest.prediction_markets            # all open MLB game markets
  python -m backtest.prediction_markets --game ARI@SEA

Series: KXMLBGAME (game winner). Ticker encodes date+teams+side; we resolve
home/away from the market title and map team codes to pipeline abbreviations.
Free, no auth. (Polymarket MLB per-game coverage is sparse — see note at bottom.)
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config
import db
from _compat import game_pk

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MLB_GAME_SERIES = "KXMLBGAME"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# Kalshi team codes / sub-titles -> pipeline abbreviations (only the ones that differ).
CODE_FIX = {"AZ": "ARI", "SD": "SDP", "SF": "SFG", "TB": "TBR", "KC": "KCR",
            "WSH": "WSN", "WAS": "WSN", "CWS": "CHW", "OAK": "ATH"}
NAME_TO_ABBR = {
    "los angeles d": "LAD", "los angeles a": "LAA", "arizona": "ARI", "atlanta": "ATL",
    "baltimore": "BAL", "boston": "BOS", "chicago c": "CHC", "chicago w": "CHW",
    "cincinnati": "CIN", "cleveland": "CLE", "colorado": "COL", "detroit": "DET",
    "houston": "HOU", "kansas city": "KCR", "miami": "MIA", "milwaukee": "MIL",
    "minnesota": "MIN", "new york m": "NYM", "new york y": "NYY", "a's": "ATH",
    "athletics": "ATH", "philadelphia": "PHI", "pittsburgh": "PIT", "san diego": "SDP",
    "san francisco": "SFG", "seattle": "SEA", "st. louis": "STL", "tampa bay": "TBR",
    "texas": "TEX", "toronto": "TOR", "washington": "WSN",
}


def _get_url(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(path: str, params: dict) -> dict:
    return _get_url(f"{KALSHI_BASE}{path}?{urllib.parse.urlencode(params)}")


def _abbr(code_or_name: str) -> str | None:
    s = (code_or_name or "").strip()
    if not s:
        return None
    up = s.upper()
    if up in config.PARK_FACTORS:           # already a pipeline abbr
        return up
    if up in CODE_FIX:
        return CODE_FIX[up]
    n = NAME_TO_ABBR.get(s.lower())
    if n:
        return n
    return CODE_FIX.get(up, up[:3])


def _ticker_date(ticker: str) -> str | None:
    """KXMLBGAME-26JUN03... -> 2026-06-03."""
    import re
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", ticker)
    if not m:
        return None
    yy, mon, dd = m.groups()
    months = {"JAN": "01", "FEB": "02", "MAR": "03", "APR": "04", "MAY": "05", "JUN": "06",
              "JUL": "07", "AUG": "08", "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12"}
    mm = months.get(mon)
    return f"20{yy}-{mm}-{dd}" if mm else None


def _game_start_ts(ticker: str) -> int | None:
    """Game start from the ticker's encoded date+time (ET) -> UTC unix seconds.
    KXMLBGAME-26MAY311920... = 2026-05-31 19:20 ET. June = EDT (UTC-4)."""
    import re
    from datetime import datetime, timezone, timedelta
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})", ticker)
    if not m:
        return None
    yy, mon, dd, hh, mm = m.groups()
    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
              "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
    if mon not in months:
        return None
    et = timezone(timedelta(hours=-4))   # EDT (MLB season)
    dt = datetime(2000 + int(yy), months[mon], int(dd), int(hh), int(mm), tzinfo=et)
    return int(dt.timestamp())


def _implied(m: dict) -> float | None:
    bid, ask = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
    if bid is not None and ask is not None and (bid or ask):
        return round((float(bid) + float(ask)) / 2, 4)
    last = m.get("last_price_dollars")
    return round(float(last), 4) if last else None


def fetch_markets(status: str, max_pages: int = 10) -> list[dict]:
    """Paginate KXMLBGAME markets for a status ('open' or 'settled')."""
    out, cursor = [], None
    for _ in range(max_pages):
        params = {"series_ticker": MLB_GAME_SERIES, "status": status, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params)
        page = data.get("markets", [])
        out.extend(page)
        cursor = data.get("cursor")
        if not cursor or not page:
            break
    return out


TO_KALSHI = {"ARI": "AZ", "SDP": "SD", "SFG": "SF", "TBR": "TB", "KCR": "KC",
             "WSN": "WSH", "CHW": "CWS", "ATH": "OAK"}
_MON = {1: "JAN", 2: "FEB", 3: "MAR", 4: "APR", 5: "MAY", 6: "JUN",
        7: "JUL", 8: "AUG", 9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC"}


def _date_compact(gdate: str) -> str:
    y, m, d = gdate.split("-")
    return f"{y[2:]}{_MON[int(m)]}{d}"


def f5_market(away: str, home: str, gdate: str) -> dict:
    """Live Kalshi First-5-innings TOTAL for a game -> {line: over_implied_mid}.
    Only liquid strikes (bid/ask spread < 0.20). Matched by date + team blob."""
    blob = TO_KALSHI.get(away, away) + TO_KALSHI.get(home, home)
    dc = _date_compact(gdate)
    out, cursor = {}, None
    for _ in range(8):
        params = {"series_ticker": "KXMLBF5TOTAL", "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params)
        for m in data.get("markets", []):
            tk = m.get("ticker", "")
            if dc not in tk or blob not in tk:
                continue
            strike = m.get("floor_strike")
            bid, ask = m.get("yes_bid_dollars"), m.get("yes_ask_dollars")
            if strike is None or bid is None or ask is None:
                continue
            bid, ask = float(bid), float(ask)
            if ask - bid > 0.20 or not (bid or ask):
                continue
            out[float(strike)] = round((bid + ask) / 2, 4)
        cursor = data.get("cursor")
        if not cursor:
            break
    return out


def backfill_history(max_pages: int = 60) -> None:
    """Pull the LARGE settled-market sample. The reliable signal here is the game
    OUTCOME (result/winner) — a big independent sample of real MLB results. The
    stored price (previous_* before settlement) is APPROXIMATE: many settled markets
    sit at the opening 0.50 default or a post-game extreme, so it is NOT a clean
    closing line. v_pm_calibration drops the 0/1 extremes; true historical closing
    prices require the candlesticks endpoint (price at occurrence_datetime) — TODO.
    The forward open-market scraper (run()) is the clean pre-game price + movement."""
    markets = fetch_markets("settled", max_pages=max_pages)
    rows = []
    for m in markets:
        ticker = m.get("ticker", "")
        gdate = _ticker_date(ticker)
        suffix = ticker.rsplit("-", 1)[-1]
        side = _abbr(suffix)
        result = m.get("result")            # 'yes' / 'no'
        # A settled market's last_price is the 0/1 settlement; the real CLOSING line
        # is previous_* (last meaningful price before resolution).
        pb, pa = m.get("previous_yes_bid_dollars"), m.get("previous_yes_ask_dollars")
        if pb is not None and pa is not None and (float(pb) or float(pa)):
            close_px = round((float(pb) + float(pa)) / 2, 4)
        else:
            pp = m.get("previous_price_dollars")
            close_px = round(float(pp), 4) if pp else None
        if not gdate or not side or close_px is None or result not in ("yes", "no"):
            continue
        # Resolve the opponent from the event title for a deterministic game_pk.
        core = (m.get("title") or "").split(" Winner")[0]
        away = home = None
        if " vs " in core:
            away, home = (_abbr(s.strip()) for s in core.split(" vs ", 1))
        gpk = game_pk(gdate, away, home) if away and home else game_pk(gdate, side, side)
        rows.append({
            "game_pk": gpk, "snapshot_time": (m.get("close_time") or m.get("expiration_time") or NOW),
            "game_date": gdate, "venue": "kalshi", "market_type": "ml", "selection": side,
            "implied_probability": close_px, "last_price": m.get("last_price_dollars"),
            "ticker": ticker, "source": "kalshi",
            "settled": True, "won": (result == "yes"), "result_value": m.get("expiration_value"),
        })
    if rows:
        db.insert("prediction_market_snapshots", rows)
    games = len({r["game_pk"] for r in rows})
    print(f"  Kalshi history: logged {len(rows)} settled contracts across ~{games} games "
          f"(price + outcome). Calibration base: select * from v_pm_calibration;")


def _unix(ts: str | None) -> int | None:
    if not ts:
        return None
    from datetime import datetime
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def _candle_close_price(candle: dict) -> float | None:
    """Mid of the bid/ask close for one candle; fall back to traded close/mean."""
    def cl(side):
        v = (candle.get(side) or {}).get("close_dollars")
        return float(v) if v not in (None, "") else None
    bid, ask = cl("yes_bid"), cl("yes_ask")
    if bid is not None and ask is not None and (bid or ask):
        return round((bid + ask) / 2, 4)
    p = candle.get("price") or {}
    for k in ("close_dollars", "mean_dollars", "previous_dollars"):
        if p.get(k) not in (None, ""):
            return round(float(p[k]), 4)
    return None


def _movement_stats(ticker: str, start_ts: int, open_ts: int, close_ts: int) -> dict | None:
    """Pre-first-pitch price trajectory from candlesticks -> open/close/high/low/
    ticks/volume + the closing candle ts. The line-movement signal base."""
    url = (f"{KALSHI_BASE}/series/{MLB_GAME_SERIES}/markets/{ticker}/candlesticks"
           f"?start_ts={open_ts}&end_ts={close_ts}&period_interval=60")
    try:
        cs = _get_url(url).get("candlesticks", [])
    except Exception:
        return None
    pre = []
    for c in cs:
        ets = c.get("end_period_ts")
        if ets is None or ets > start_ts:          # only pre-first-pitch candles
            continue
        px = _candle_close_price(c)
        if px is not None and 0 < px < 1:
            vol = c.get("volume_fp")
            pre.append((ets, px, float(vol) if vol not in (None, "") else 0.0))
    if not pre:
        return None
    pre.sort(key=lambda x: x[0])
    prices = [p for _, p, _ in pre]
    return {
        "open": prices[0], "close": prices[-1], "close_ts": pre[-1][0],
        "high": max(prices), "low": min(prices), "n": len(prices),
        "volume": round(sum(v for _, _, v in pre), 2),
    }


def backfill_candlesticks(max_games: int = 400, sleep_s: float = 0.03) -> None:
    """Large-sample CLEAN closing-price + outcome dataset: for each settled MLB game
    market, pull candlesticks and take the price of the last candle before first
    pitch (occurrence_datetime). One market per game (the other side is the
    complement). This is the real price->outcome backtest base."""
    import time
    markets = fetch_markets("settled", max_pages=60)
    # Pull BOTH sides of every game (each contract is an independent price->outcome
    # observation) for the largest, most symmetric calibration sample.
    picks = markets[:max_games * 2]
    print(f"  Pulling closing lines for {len(picks)} contracts (candlesticks)...")

    rows, done = [], 0
    for m in picks:
        ticker = m.get("ticker", "")
        # Cutoff = game START (from the ticker), not occurrence_datetime (~game end).
        start = _game_start_ts(ticker)
        ot, ct = _unix(m.get("open_time")), _unix(m.get("close_time"))
        gdate, suffix, result = _ticker_date(ticker), ticker.rsplit("-", 1)[-1], m.get("result")
        side = _abbr(suffix)
        if not (start and ot and ct and gdate and side and result in ("yes", "no")):
            continue
        mv = _movement_stats(ticker, start, ot, ct)
        done += 1
        if not mv:
            continue
        core = (m.get("title") or "").split(" Winner")[0]
        away = home = None
        if " vs " in core:
            away, home = (_abbr(s.strip()) for s in core.split(" vs ", 1))
        gpk = game_pk(gdate, away, home) if away and home else game_pk(gdate, side, side)
        from datetime import datetime, timezone
        rows.append({
            "game_pk": gpk, "snapshot_time": datetime.fromtimestamp(mv["close_ts"], timezone.utc).isoformat(),
            "game_date": gdate, "venue": "kalshi", "market_type": "ml", "selection": side,
            "implied_probability": mv["close"], "open_prob": mv["open"],
            "delta": round(mv["close"] - mv["open"], 4), "high_prob": mv["high"],
            "low_prob": mv["low"], "n_ticks": mv["n"], "volume": mv["volume"],
            "last_price": m.get("last_price_dollars"),
            "ticker": ticker, "source": "kalshi-candlestick",
            "settled": True, "won": (result == "yes"), "result_value": m.get("expiration_value"),
        })
        if sleep_s:
            time.sleep(sleep_s)

    if rows:
        db.insert("prediction_market_snapshots", rows)
    print(f"  Clean closing lines: {len(rows)} games (of {done} fetched). "
          f"Calibration: select * from v_pm_calibration where source='kalshi-candlestick';")


def run(only_game: str | None = None) -> None:
    markets = fetch_markets("open")
    if not markets:
        raise SystemExit("  No open KXMLBGAME markets returned.")

    # Index our games by (date, unordered team pair) -> the canonical game_pk, so
    # prediction prices map to the SAME game_pk our odds/model use (robust to any
    # away/home-order disagreement between Kalshi's title and our slate).
    games_idx: dict[tuple, int] = {}
    for g in db.select("games", "?select=game_pk,game_date,home_team,away_team"):
        d = str(g.get("game_date") or "")[:10]
        a, h = g.get("away_team"), g.get("home_team")
        if d and a and h:
            games_idx[(d, frozenset((a, h)))] = g["game_pk"]

    # Group by event so we can resolve away/home from the title once per game.
    by_event: dict[str, list] = {}
    for m in markets:
        by_event.setdefault(m.get("event_ticker"), []).append(m)

    rows, games_seen = [], set()
    for event, mkts in by_event.items():
        title = (mkts[0].get("title") or "")          # "Away vs Home Winner?"
        core = title.split(" Winner")[0]
        if " vs " not in core:
            continue
        away_name, home_name = (s.strip() for s in core.split(" vs ", 1))
        away, home = _abbr(away_name), _abbr(home_name)
        if not away or not home:
            continue
        gdate = _ticker_date(event) or _ticker_date(mkts[0].get("ticker", ""))
        if not gdate:
            continue
        if only_game and f"{away}@{home}".upper() != only_game.upper():
            continue
        # Prefer our canonical game_pk (match by team pair + date); fall back to the
        # deterministic key from the title so future games still log + join later.
        gpk = games_idx.get((gdate, frozenset((away, home)))) or game_pk(gdate, away, home)
        games_seen.add(f"{away}@{home}")

        for m in mkts:
            # Ticker suffix is a clean team code (CWS, AZ, ...); prefer it over the
            # sub-title ("Chicago" is ambiguous between Cubs/White Sox).
            suffix = m.get("ticker", "").rsplit("-", 1)[-1]
            side = _abbr(suffix) or _abbr(m.get("yes_sub_title"))
            imp = _implied(m)
            if side is None or imp is None:
                continue
            rows.append({
                "game_pk": gpk, "snapshot_time": NOW, "venue": "kalshi",
                "market_type": "ml", "selection": side, "line": None,
                "yes_bid": m.get("yes_bid_dollars"), "yes_ask": m.get("yes_ask_dollars"),
                "last_price": m.get("last_price_dollars"), "implied_probability": imp,
                "volume": m.get("volume_fp"), "open_interest": m.get("open_interest_fp"),
                "liquidity": m.get("liquidity_dollars"), "ticker": m.get("ticker"),
                "source": "kalshi",
            })

    if rows:
        db.insert("prediction_market_snapshots", rows)
    print(f"  Kalshi: logged {len(rows)} contract prices across {len(games_seen)} games "
          f"(venue=kalshi, market=ml) at {NOW}.")
    if rows:
        print("  Re-run through the day to accumulate movement; the closing price is a "
              "second CLV anchor. Cross-reference: select * from v_market_consensus;")


def main():
    p = argparse.ArgumentParser(description="Kalshi MLB prediction-market ingestion.")
    p.add_argument("--game", help='Limit to "AWAY@HOME"')
    p.add_argument("--history", action="store_true",
                   help="Backfill settled markets (outcomes; approximate price)")
    p.add_argument("--candlesticks", action="store_true",
                   help="Backfill CLEAN closing lines (candlestick at first pitch) + outcome")
    p.add_argument("--pages", type=int, default=60, help="Max history pages (200/page)")
    p.add_argument("--games", type=int, default=400, help="Max games for candlestick backfill")
    args = p.parse_args()
    if args.candlesticks:
        backfill_candlesticks(max_games=args.games)
    elif args.history:
        backfill_history(max_pages=args.pages)
    else:
        run(args.game)


if __name__ == "__main__":
    main()
