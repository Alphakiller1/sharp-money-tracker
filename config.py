"""
Standalone config for the Bet Evaluator project.

This project is self-contained. It READS data from the mlbma_pipeline output
folder (never writes to it) and WRITES bet analyses into the ChaseAnalytics-Brain
vault. mlbma_pipeline is treated as a read-only data source.
"""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a local .env (no external dependency)."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv()

# ── External paths ─────────────────────────────────────────────────────────
# Read-only MLB data source (mlbma_pipeline output). Override with env var if the
# pipeline moves.
PIPELINE_DATA_DIR = Path(
    os.getenv("MLBMA_DATA_DIR", r"C:\Users\user\Documents\mlbma-pipeline\data")
)

# Vault the evaluator writes analyses into.
VAULT_ROOT = Path(
    os.getenv(
        "CHASE_VAULT_ROOT",
        r"C:\Users\chase\Documents\chase-analytics-brain\ChaseAnalytics-Brain",
    )
)
BET_HISTORY_DIR = VAULT_ROOT / "13-Bet-History"

# ── Model anchors (documented heuristic constants; calibrate over time) ────────
# Defaults; refreshed at runtime from game_results.csv when present.
LEAGUE_RUNS_PER_TEAM = 4.58      # mean team runs / game
LEAGUE_FIP = 4.20               # neutral SP FIP anchor
HOME_BASE_WINP = 0.540          # empirical home win rate
AWAY_BASE_WINP = 0.460
TOTAL_RUNS_SD = 4.79            # SD of total runs / game
TEAM_RUNS_SD = 3.33             # SD of one team's runs / game
MARGIN_SD = 4.40                # SD of run margin (home - away)
HFA_RUNS = 0.15                 # small home-field run bump

# Mapping strength.
OSI_RUN_SENSITIVITY = 0.9       # off_factor = 1 + (OSI-50)/100 * this
SP_FIP_WEIGHT = 0.70            # SP covers ~70% of run prevention; rest = bullpen
BULLPEN_WEIGHT = 1 - SP_FIP_WEIGHT  # bullpen share of opponent run prevention
LEAGUE_BULLPEN_ERA = 4.05       # fallback anchor; runtime uses team_profiles mean
BULLPEN_IR_SENSITIVITY = 0.004  # pen-factor nudge per point of IR-scored% vs league
OFF_FACTOR_CLIP = (0.55, 1.60)
PITCH_FACTOR_CLIP = (0.60, 1.70)
# Early-season inputs (esp. SP FIP) are noisy; regress each factor toward 1.0.
REGRESSION_TO_MEAN = 0.25
# Edges above this almost always mean stale/noisy inputs or a misread line.
IMPLAUSIBLE_EDGE = 0.15

# Confidence tiers -> unit sizing (mirrors 06-Betting-Logic/Unit-Sizing note).
# (edge_min as probability points, label, unit_range)
CONFIDENCE_TIERS = [
    (0.080, "Strong", "1.0-2.0u"),
    (0.045, "Standard", "0.5-1.0u"),
    (0.020, "Lean", "0.25-0.5u"),
    (-1.0,  "Pass", "0u"),
]

# 2026 park factors (offense environment; 1.0 = neutral). Source: mlbma_pipeline.
PARK_FACTORS = {
    "COL": 1.38, "BOS": 1.12, "CIN": 1.10, "TEX": 1.08, "PHI": 1.07,
    "NYY": 1.06, "CHC": 1.05, "MIL": 1.04, "ATL": 1.03, "HOU": 1.02,
    "LAD": 1.01, "NYM": 1.00, "STL": 1.00, "MIN": 0.99, "DET": 0.99,
    "TOR": 0.98, "BAL": 0.98, "ARI": 0.97, "SFG": 0.97, "SEA": 0.96,
    "CLE": 0.96, "PIT": 0.95, "WSN": 0.95, "KCR": 0.95, "MIA": 0.94,
    "TBR": 0.94, "LAA": 0.93, "SDP": 0.92, "CHW": 0.91, "ATH": 0.90,
}


def park_factor_for_team(team: str) -> float:
    return PARK_FACTORS.get(str(team).strip().upper(), 1.0)


# ── Market data (odds scraper) ───────────────────────────────────────────────
# Local store for scraped odds (this project's own data dir).
EVAL_DATA_DIR = Path(__file__).resolve().parent / "data"
ODDS_LATEST_CSV = EVAL_DATA_DIR / "odds_latest.csv"
ODDS_HISTORY_CSV = EVAL_DATA_DIR / "odds_history.csv"

# The Odds API (the-odds-api.com). Free tier: get a key, set ODDS_API_KEY env var
# (or paste it into ODDS_API_KEY_FALLBACK below). Never commit a real key.
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
# Refuse paid fetches when the cached remaining quota drops below this floor
# (0 disables the guard). The cache is written after every API response.
ODDS_API_MIN_REMAINING = int(os.getenv("ODDS_API_MIN_REMAINING", "20"))

# Optional ops webhook for plain-text warnings (stale slate, etc.).
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ODDS_SPORT_KEY = "baseball_mlb"
ODDS_REGIONS = "us"            # us|us2|uk|eu|au
ODDS_FORMAT = "american"
# Game markets pulled every fetch (cheap: 1 request for the whole slate).
ODDS_GAME_MARKETS = "h2h,spreads,totals"
# Player-prop / team-total markets (per-event requests; cost more credits).
ODDS_PROP_MARKETS = "batter_hits,batter_total_bases,pitcher_strikeouts,team_totals"
# Restrict to specific books, or "" for all US books (enables best-price shopping).
ODDS_BOOKMAKERS = ""          # e.g. "draftkings,fanduel,betmgm"

# Sharp-money tracking. Regions incl. EU so Pinnacle (the sharp reference) is pulled.
ODDS_SHARP_REGIONS = "us,eu"
# Books treated as "sharp" (move first, low-vig, respected by the market).
SHARP_BOOKS = {"pinnacle", "betonlineag", "lowvig", "bookmaker", "circasports"}
# Divergence (de-vig sharp consensus minus soft consensus) to flag a sharp lean.
SHARP_DIVERGENCE_MIN = 0.02
# Above this, the gap is almost always a stale/mismatched line (esp. runline across
# books at different numbers), not real sharp money — treat as an artifact and skip.
SHARP_DIVERGENCE_MAX = 0.12
# Books moving the same way between snapshots to flag a steam move.
STEAM_BOOK_MIN = 3

# Map The Odds API full team names -> pipeline abbreviations.
TEAM_NAME_TO_ABBR = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CHW",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KCR",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Athletics": "ATH", "Oakland Athletics": "ATH",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SDP",
    "San Francisco Giants": "SFG", "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TBR", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSN",
}


def team_abbr(name: str) -> str:
    """Full team name -> abbreviation; pass through if already an abbr."""
    n = str(name).strip()
    if n.upper() in PARK_FACTORS:        # already an abbreviation
        return n.upper()
    return TEAM_NAME_TO_ABBR.get(n, n.upper()[:3])


# ── Supabase (backtest / historical truth layer) ─────────────────────────────
# Create a project at https://supabase.com, then put these in .env (gitignored):
#   SUPABASE_URL=https://<ref>.supabase.co
#   SUPABASE_KEY=<service-role key>          # writes; keep secret
#   SUPABASE_DB_URL=postgresql://...         # direct psql connection (migrations)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")              # service-role (writes)
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")  # anon (reads)
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")

# Bumped when a metric formula changes (stamped onto every snapshot).
METRIC_VERSION = os.getenv("MLBMA_METRIC_VERSION", "2026.05")
MODEL_VERSION = os.getenv("BET_MODEL_VERSION", "v1-expected-runs")
