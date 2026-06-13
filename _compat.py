"""
Small self-contained helpers so the sharp-money tracker runs standalone (no
dependency on the bet-evaluator package). Mirrors the few functions the tracker
borrowed from there.
"""

from __future__ import annotations

import os
import zlib
from datetime import date, datetime, timedelta, timezone

import pandas as pd

import config

TODAY = date.today().isoformat()

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # no IANA tz database (Windows without the tzdata package)
    _ET = None


def eastern(dt: datetime) -> datetime:
    """Attach US/Eastern (DST-aware); fixed EDT fallback if no tz database."""
    return dt.replace(tzinfo=_ET if _ET is not None else timezone(timedelta(hours=-4)))


def load(filename: str) -> pd.DataFrame | None:
    """Read a CSV from the pipeline data dir (read-only source)."""
    path = os.path.join(config.PIPELINE_DATA_DIR, filename)
    if not os.path.exists(path):
        print(f"  WARNING: {filename} not found in {config.PIPELINE_DATA_DIR}")
        return None
    return pd.read_csv(path)


def american_to_implied(odds: int) -> float:
    return 100.0 / (odds + 100.0) if odds > 0 else (-odds) / (-odds + 100.0)


def game_pk(d: str, away: str, home: str) -> int:
    """Deterministic surrogate key from date+teams (stable across sources)."""
    return zlib.crc32(f"{d}|{away}|{home}".encode())


def scheduled_start(d: str, time_str: str) -> str | None:
    """Slate time like '12:15 PM ET' -> ISO timestamp with the correct ET offset."""
    t = str(time_str).replace("ET", "").strip()
    try:
        hm = datetime.strptime(t, "%I:%M %p")
        y, mo, dd = (int(x) for x in d.split("-"))
        return eastern(datetime(y, mo, dd, hm.hour, hm.minute)).isoformat()
    except ValueError:
        return None


_FRESHNESS_WARNED = False


def slate_date() -> str | None:
    """Date of the current slate: Slate_Date column of today_matchups.csv,
    falling back to the file's mtime date. None if the file is missing."""
    path = os.path.join(config.PIPELINE_DATA_DIR, "today_matchups.csv")
    if not os.path.exists(path):
        return None
    try:
        d = str(pd.read_csv(path, nrows=1).iloc[0].get("Slate_Date", "")).strip()[:10]
        if len(d) == 10:
            return d
    except Exception:
        pass
    return date.fromtimestamp(os.path.getmtime(path)).isoformat()


def check_slate_freshness(context: str = "this output") -> bool:
    """True if the pipeline slate is today's. Stale/missing -> loud banner once
    per process (+ optional Discord webhook ping). Warns, never crashes."""
    global _FRESHNESS_WARNED
    d = slate_date()
    if d == TODAY:
        return True
    if not _FRESHNESS_WARNED:
        _FRESHNESS_WARNED = True
        msg = (f"STALE SLATE: today_matchups.csv is dated {d or 'MISSING'} but today is "
               f"{TODAY}. Run the mlbma pipeline before trusting {context}.")
        bar = "!" * 74
        print(f"\n  {bar}\n  !! {msg}\n  {bar}\n")
        if getattr(config, "DISCORD_WEBHOOK_URL", ""):
            try:
                import json
                import urllib.request
                req = urllib.request.Request(
                    config.DISCORD_WEBHOOK_URL,
                    data=json.dumps({"content": f":warning: {msg}"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10)
            except Exception as exc:
                print(f"  (stale-slate webhook post failed: {exc})")
    return False
