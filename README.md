# Sharp Money Tracker — Market-Edge Engine

Finds where the betting market is **consistently beatable** and proves it with
**profit, not probability points**. A real-money exchange (Kalshi) and sharp books
(Pinnacle, BetOnline, LowVig) are de-vigged to true probabilities, line movement is
tracked open→close, settled against outcomes, and every candidate edge is gated by
quant tests before it is called tradeable.

**Live dashboard:** `docs/` is served by GitHub Pages — it visually articulates the
tool's pipeline, the quant gating, and the current findings.

## What it does
1. **Ingest** — sharp + soft sportsbook odds (The Odds API, `us,eu` so Pinnacle is
   included) and Kalshi MLB contract prices, timestamped so movement accumulates.
2. **De-vig** — strip the hold to a true probability per book; sharp vs soft consensus.
3. **Signal** — sharp-vs-soft divergence, line movement (open→close), steam.
4. **Settle** — grade vs final outcomes; compute CLV (did the line move our way).
5. **Quant gate** — ROI at the entry price · Kelly stake · bootstrap CI · Benjamini-
   Hochberg false-discovery control. Only economically meaningful + robust + FDR-
   surviving segments surface.

## The quant logic (why these and not "minute edges")
- **ROI at entry, not the close** — the closing line is near-efficient; profit is the
  return at the *open*, before sharp money moves it.
- **CLV** — beating the closing number is the best-validated predictor of long-run profit.
- **Kelly criterion** — growth-optimal stake; tiny edges → tiny Kelly → filtered out.
- **Bootstrap CI** — non-parametric bound on ROI (payoffs are skewed); trust only when the lower bound clears the hurdle.
- **Benjamini-Hochberg FDR** — we scan many segments, so we control false discovery instead of cherry-picking.
- **Honest-empty** — if nothing clears the bar, it reports nothing.

## Current finding (settled MLB sample)
Entering at the **open on steam-up sides** is profitable and robust:

| Vulnerability | ROI/unit | 95% bootstrap LB | Kelly | CLV |
|---|---|---|---|---|
| Underdog + steam-up | +47.9% | +16.2% | 25% | +8.5 |
| Steam-up ≥4pt | +23.1% | +6.4% | 19% | +9.3 |
| Steam-up ≥2pt | +20.8% | +5.7% | 16% | +7.6 |

Fading down-moves (−19.5% ROI) and thin-market moves are correctly rejected as noise.

## Run it
```bash
pip install -r requirements.txt
cp .env.example .env          # fill SUPABASE_* and ODDS_API_KEY
python -m apply_schema        # idempotent Supabase schema

python sharp_tracker.py                       # live sharp signals (pre-game)
python -m prediction_markets --candlesticks   # backfill closing-line + outcome sample
python -m market_edge                         # the profit-focused vulnerability scan
python -m scenarios                           # parameterized react-scenarios
python export_dashboard.py                    # refresh docs/data.json for the dashboard
```

## Architecture
Reads MLB metric CSVs from the `mlbma_pipeline` data dir (read-only) and writes
signals/observations/findings to a shared **Supabase** warehouse. Part of the Chase
Analytics ecosystem (pipeline → evaluator → **sharp tracker** → warehouse → dashboard).

*Educational analysis only — not betting advice.*

## Projection tracking

Pitcher prop snapshots → Supabase for MAE / lean-hit reporting. See [`docs/PROJECTION_TRACKING.md`](docs/PROJECTION_TRACKING.md).
