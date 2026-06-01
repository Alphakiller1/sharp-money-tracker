"""
Phase 4b — settle sharp observations against final outcomes.

For every unsettled sharp_observation whose game has an outcome, grade whether the
sharp side won/covered and mark it settled. This is what turns logged sharp leans
into a track record (which book / market / time / condition actually wins).

    python -m backtest.import_outcomes   # first, so outcomes exist
    python -m backtest.settle_sharp
"""

from __future__ import annotations

import config


def _grade(o, home, away, hr, ar, total, margin, winner):
    """Return (won, push) for one observation given the outcome, or (None, None)."""
    mt, sel, line, role = o["market_type"], o["selection"], o["line"], o["side_role"]
    if mt == "ml":
        return (winner == sel, False)
    if mt == "total":
        if total is None or line is None:
            return (None, None)
        if total == line:
            return (None, True)
        return ((total > line) if sel == "over" else (total < line), False)
    if mt == "team_total":
        if line is None:
            return (None, None)
        team, _, ou = sel.partition("_")
        tr = hr if team == home else ar
        if tr is None:
            return (None, None)
        if tr == line:
            return (None, True)
        return ((tr > line) if ou == "over" else (tr < line), False)
    if mt == "runline":
        if margin is None:
            return (None, None)
        team_margin = margin if sel == home else -margin
        rl = -1.5 if role == "fav" else 1.5
        return (team_margin + rl > 0, False)
    return (None, None)


def run():
    import psycopg2
    conn = psycopg2.connect(config.SUPABASE_DB_URL, connect_timeout=20)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute("select game_pk, home_team, away_team from games")
    games = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.execute("select game_pk, home_runs, away_runs, total_runs, margin_home, winner_team from game_outcomes")
    outs = {r[0]: r[1:] for r in cur.fetchall()}
    cur.execute("select obs_id, game_pk, market_type, selection, line, side_role "
                "from sharp_observations where settled = false")
    obs = cur.fetchall()

    updates = []
    for obs_id, gpk, mt, sel, line, role in obs:
        if gpk not in outs or gpk not in games:
            continue
        home, away = games[gpk]
        hr, ar, total, margin, winner = outs[gpk]
        won, push = _grade({"market_type": mt, "selection": sel,
                            "line": float(line) if line is not None else None,
                            "side_role": role}, home, away, hr, ar, total, margin, winner)
        if won is None and not push:
            continue
        updates.append((won, bool(push), obs_id))

    if updates:
        cur.executemany(
            "update sharp_observations set settled=true, won=%s, push=%s where obs_id=%s",
            updates)
    conn.close()
    print(f"  Settled {len(updates)} sharp observations "
          f"(of {len(obs)} unsettled; matched to outcomes).")


if __name__ == "__main__":
    run()
