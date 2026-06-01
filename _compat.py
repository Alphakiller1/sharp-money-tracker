"""
Small self-contained helpers so the sharp-money tracker runs standalone (no
dependency on the bet-evaluator package). Mirrors the few functions the tracker
borrowed from there.
"""

from __future__ import annotations

import os
import zlib
from datetime import date, datetime, timedelta

import pandas as pd

import config

TODAY = date.today().isoformat()


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
    """Slate time like '12:15 PM ET' -> ISO timestamp (ET = -04:00 in season)."""
    t = str(time_str).replace("ET", "").strip()
    try:
        hm = datetime.strptime(t, "%I:%M %p")
        return f"{d}T{hm.hour:02d}:{hm.minute:02d}:00-04:00"
    except ValueError:
        return None
