# Projection Tracking

Pitcher prop projections from `export_boards.py` are snapshotted to Supabase for post-game accuracy reporting.

## Enable Supabase

1. Create a project at [supabase.com](https://supabase.com).
2. Run the migration in the SQL editor:
   - `mlbma-pipeline/supabase/migrations/0002_projection_tracking.sql`
3. Add to `sharp-money-tracker/.env`:

```env
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_KEY=<service-role-key>
MLBMA_DATA_DIR=C:/Users/user/Documents/mlbma-pipeline/data
```

`SUPABASE_KEY` is the **service role** key (writes). Push skips gracefully if unset.

## Tables

| Table | Purpose |
|-------|---------|
| `projection_snapshots` | Pre-game projections, leans, `factors_json`, `props_json` |
| `projection_outcomes` | Actual K/BB/ER/outs/IP from `sp_gamelog.csv` |
| `projection_accuracy` | Per-prop MAE, lean hit, Brier-style score |

Views: `v_projection_accuracy_summary`, `v_projection_factor_layers`.

## Workflow

### Fast daily (reuse fresh MLBMA `data/` CSVs)

```bash
# mlbma-pipeline — compute-only / targeted scrapes (skip what's fresh today)
cd C:/Users/user/Documents/mlbma-pipeline
crawl_env/Scripts/python.exe -m scripts.sync_from_cache

# sharp-money — slate refresh only if stale, then export + Supabase push
crawl_env/Scripts/python.exe -m scripts.refresh_sharp_money
# or skip MLBMA scrapes entirely when slate is current:
crawl_env/Scripts/python.exe -m scripts.refresh_sharp_money --export-only
```

`refresh_sharp_money` never runs pitch_mix, sp_gamelog, batter_splits, or FanGraphs.
Those are MLBMA pipeline jobs (`sync_from_cache` / `finish_pipeline_smart`).

### Full pipeline (cold start or stale season files)

```bash
cd C:/Users/user/Documents/mlbma-pipeline
crawl_env/Scripts/python.exe -m scripts.finish_pipeline_smart
# or: crawl_env/Scripts/python.exe -m pipeline.main
```

### Boards + projection tracking

```bash
# Refresh boards + push snapshots (skip push if no key)
python export_boards.py

# Or push explicitly
python push_projections.py

# After games finalize
python scripts/reconcile_projection_outcomes.py --date 2026-06-25

# Summary table
python scripts/projection_report.py
```

## L14 pitch mix (mlbma-pipeline)

`scrapers/scrape_pitch_mix.py` exports `pitch_mix_*_l14.csv` with HTTP retry via `core/http_retry.py`.
`sharp-money-tracker/_compat.load_pitch_mix` prefers L14 files when present.
