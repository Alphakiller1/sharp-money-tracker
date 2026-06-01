"""
Apply backtest/schema.sql to Supabase Postgres via SUPABASE_DB_URL.

Idempotent (create ... if not exists), so safe to re-run whenever the schema grows.

    python -m backtest.apply_schema
"""

from __future__ import annotations

from pathlib import Path

import config

SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def run():
    if not config.SUPABASE_DB_URL:
        raise SystemExit("SUPABASE_DB_URL not set (see .env).")
    try:
        import psycopg2
    except ImportError:
        raise SystemExit("Install the driver:  pip install psycopg2-binary")

    sql = SCHEMA.read_text(encoding="utf-8")
    try:
        conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=20)
    except Exception as e:
        raise SystemExit(
            f"Could not connect: {e}\n"
            "  The DIRECT connection (db.<ref>.supabase.co) is IPv6-only and often\n"
            "  unreachable. Use the SESSION POOLER string instead (Connect -> Session\n"
            "  pooler), which works over IPv4.")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute("notify pgrst, 'reload schema'")   # refresh PostgREST cache
    conn.close()
    print("  Schema applied (idempotent) + PostgREST cache reloaded.")


if __name__ == "__main__":
    run()
