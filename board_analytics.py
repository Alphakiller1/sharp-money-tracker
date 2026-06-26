"""
Pitcher + general-market analytics for the sharp dashboard.

Regression/progression is NOT just ERA-vs-FIP. We extrapolate the full luck stack
from the mlbma pipeline, then layer TODAY'S MATCHUP for starters:
  - ERA vs FIP and vs xFIP        (DIPS skill gap)
  - BABIP vs ~.295                (balls-in-play luck, from the game log)
  - LOB% / strand vs ~72%         (sequencing luck, from the game log)
  - FIP vs xFIP                   (HR-suppression luck)
  - strength of schedule faced    (soft slate inflates results -> mean-reverts)
  - recent K%/BB% trend           (real skill change, not luck)
  - workload / pitch-count fatigue (stuff erosion risk)
  Matchup overlay (today's starters only):
  - projected lineup platoon mix  (LHH/RHH vs SP hand from Today_Lineups)
  - opponent split-adjusted OSI   (from Today_Matchups)
  - opponent wRC+ vs SP hand      (metrics_vs_RHP / metrics_vs_LHP)
  - split FIP weighted to lineup  (sp_vs_LHH / sp_vs_RHH)
  - ballpark factor               (home stadium run environment)
  - weather                       (wind/temp/dome from today_weather.csv)
  - pitch mix vs opp batting      (Statcast usage x whiff/CSW/xwOBA overlap)

Output drives a Pitcher board (regression + K/BB/Outs/ER prop leans) and a
General-Markets board (ML/Total/F5 lean from starter reg/prog + bullpen fatigue +
splits + opponent offense).
"""

from __future__ import annotations

import unicodedata
from datetime import date
from typing import Optional

import pandas as pd

import config
from _compat import check_slate_freshness, load, load_pitch_mix
import pitcher_model_layers as pml

# League baselines
LG_BABIP, LG_LOB, LG_HR9, LG_OOR, LG_ERA = 0.295, 0.72, 1.15, 44.0, 4.10
LG_LHH_SHARE = 0.42  # typical share of LHH in a lineup vs RHP
RECENT_N = 3
VERDICT_THRESHOLD = 0.6
# Matchup luck weights (offense moved to ER channel — no OSI/wRC double-count on K/BB).
MATCHUP_W_PLATOON = 0.38
MATCHUP_W_SPLIT_FIP = 0.22
MATCHUP_W_PARK = 0.22
OSI_TIER_SKILL_BLEND = 0.35
PITCHSCORE_SKILL_BLEND = 0.25
# Pitch-mix overlay (~10–15% of matchup adj; complements platoon/OSI — not a second wRC+/split-FIP pass).
PITCH_MIX_MATCHUP_W = 0.15
PITCH_MIX_LUCK_SCALE = 2.8   # luck runs per unit score (subtracted: +score favors SP → dampens regression)
PITCH_MIX_K_SCALE = 11.0     # K% pct-pts per unit matchup score (+ favors SP)
PITCH_MIX_BB_SCALE = 3.5     # BB% pct-pts per unit score (applied as −score×scale)
PITCH_MIX_ER_SCALE = 0.07    # ER multiplier per unit score
PITCH_MIX_OUTS_SCALE = 0.04  # outs multiplier per unit score
PITCH_MIX_MIN_USAGE = 3.0    # ignore pitch types below this % of arsenal
SKIP_PITCH_TYPES = frozenset({"UNK", "PO", "EP", "FA"})


def _factor(key: str, label: str, value, note: str = "") -> dict:
    """Keyed submetric for factors[] — positive pitch_mix favors pitcher; matchup adj uses inverted sign."""
    return {"key": key, "label": label, "value": value, "note": note or ""}


def _num(v):
    try:
        f = float(str(v).replace("%", ""))
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _norm_name(s: str) -> str:
    """Accent-insensitive name key for slate ↔ profile matching."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _registry_name_norm(full_name: str) -> str:
    """FanGraphs/Savant 'Last, First' -> norm key matching slate names."""
    s = str(full_name or "").strip()
    if "," in s:
        last, _, first = s.partition(",")
        s = f"{first.strip()} {last.strip()}"
    return _norm_name(s)


def _norm_pitch_type(code: str) -> str:
    return str(code or "UNK").strip().upper()[:4]


def _pct(v) -> float | None:
    x = _num(v)
    if x is None:
        return None
    return x * 100 if x <= 1.5 else x


def _standard_to_profile(row, *, team: str | None = None, hand: str | None = None) -> dict:
    """Map FanGraphs sp_standard row into sp_profiles-shaped dict."""
    ip = _num(row.get("IP")) or 0.0
    g = int(_num(row.get("G")) or 1)
    return {
        "pitcher_name": str(row.get("Name", "")).strip(),
        "pitcher_team": (team or str(row.get("Tm", ""))).upper(),
        "pitcher_hand": str(hand or "R").upper()[:1],
        "starts": g,
        "avg_IP": round(ip / max(g, 1), 1) if ip else 5.3,
        "ERA": _num(row.get("ERA")),
        "FIP": _num(row.get("FIP")),
        "xFIP": _num(row.get("xFIP")),
        "K_pct": _pct(row.get("K%")),
        "BB_pct": _pct(row.get("BB%")),
        "_standard_babip": _num(row.get("BABIP")),
        "_standard_lob": _num(row.get("LOB%")),
        "_source": "standard",
    }


def _build_pitcher_index(profiles: pd.DataFrame | None, standard: pd.DataFrame | None) -> dict[str, list[tuple[int, dict]]]:
    """norm_name -> [(priority, profile)] — profiles beat standard."""
    index: dict[str, list[tuple[int, dict]]] = {}

    def put(p: dict, priority: int) -> None:
        nm = str(p.get("pitcher_name") or "").strip()
        if not nm:
            return
        index.setdefault(_norm_name(nm), []).append((priority, dict(p)))

    if profiles is not None and not profiles.empty:
        for _, r in profiles.iterrows():
            d = r.to_dict()
            d["pitcher_name"] = str(d.get("pitcher_name", "")).strip()
            d["_source"] = "profiles"
            put(d, 0)
    if standard is not None and not standard.empty:
        df = standard
        if "Season" in df.columns:
            df = df[df["Season"] == df["Season"].max()]
        for _, r in df.iterrows():
            put(_standard_to_profile(r), 1)

    # FanGraphs' playerId is not an MLB Stats API ID, so resolve fallback
    # profiles through MLBMA's registry before exporting CDN headshot URLs.
    registry = load("player_registry.csv")
    registry_ids: dict[str, list[tuple[str, int]]] = {}
    if registry is not None and not registry.empty:
        for _, r in registry.iterrows():
            player_id = _num(r.get("player_id"))
            if player_id is None:
                continue
            registry_ids.setdefault(_norm_name(r.get("full_name")), []).append(
                (str(r.get("team_abbr", "")).upper(), int(player_id))
            )
    for nk, entries in index.items():
        matches = registry_ids.get(nk, [])
        for _, profile in entries:
            if _num(profile.get("pitcher_id")) is not None or not matches:
                continue
            team = str(profile.get("pitcher_team", "")).upper()
            team_match = next((pid for tm, pid in matches if tm == team), None)
            if team_match is not None or len(matches) == 1:
                profile["pitcher_id"] = team_match if team_match is not None else matches[0][1]
    for nk in index:
        index[nk].sort(key=lambda x: x[0])
    return index


def _resolve_pitcher(
    name: str,
    team: str | None,
    hand: str | None,
    index: dict[str, list[tuple[int, dict]]],
) -> dict | None:
    """Find the best profile for a slate starter (profiles first, then sp_standard)."""
    nk = _norm_name(name)
    cands = [p for _, p in index.get(nk, [])]
    if team:
        team = str(team).upper()
        team_cands = [p for p in cands if str(p.get("pitcher_team", "")).upper() == team]
        if team_cands:
            cands = team_cands
    if not cands:
        return None
    p = dict(cands[0])
    p["pitcher_name"] = str(name).strip()
    if team:
        p["pitcher_team"] = str(team).upper()
    if hand:
        p["pitcher_hand"] = str(hand).upper()[:1]
    return p


def _gamelog_slice(gamelog: pd.DataFrame | None, name: str) -> pd.DataFrame:
    if gamelog is None or gamelog.empty:
        return pd.DataFrame()
    col = "pitcher_name"
    if col not in gamelog.columns:
        return pd.DataFrame()
    nk = _norm_name(name)
    return gamelog[gamelog[col].astype(str).apply(_norm_name) == nk]


def _glfactors_for(name: str, gamelog: pd.DataFrame | None, profile: dict) -> dict:
    gf = _gamelog_factors(_gamelog_slice(gamelog, name))
    if gf.get("babip") is None and profile.get("_standard_babip") is not None:
        gf["babip"] = profile["_standard_babip"]
    if gf.get("lob") is None and profile.get("_standard_lob") is not None:
        lob = profile["_standard_lob"]
        if lob > 1.5:
            lob /= 100.0
        gf["lob"] = lob
    return gf


def _parse_today_starters(matchups: pd.DataFrame | None) -> dict[str, tuple]:
    """norm_name -> (display_name, team, opp, hand, is_home, matchup_row)."""
    today_sp: dict[str, tuple] = {}
    if matchups is None:
        return today_sp
    for _, g in matchups.iterrows():
        a, h = str(g.get("Away", "")).upper(), str(g.get("Home", "")).upper()
        gdict = g.to_dict()
        for sp_col, hand_col, team, opp, is_home in (
            ("Away_SP", "Away_Hand", a, h, False),
            ("Home_SP", "Home_Hand", h, a, True),
        ):
            nm = str(g.get(sp_col, "")).strip()
            if nm and nm.lower() != "tbd":
                today_sp[_norm_name(nm)] = (
                    nm, team, opp, str(g.get(hand_col, "R")).upper()[:1], is_home, gdict,
                )
    return today_sp


def _pitcher_row(
    profile: dict,
    *,
    display_name: str,
    team: str,
    opp: str,
    hand: str,
    is_today: bool,
    gamelog: pd.DataFrame | None,
    lineups: pd.DataFrame | None,
    weather: pd.DataFrame | None,
    split_cache: dict,
    matchup_row: dict | None = None,
    is_home: bool = False,
    pitch_mix_indexes: tuple | None = None,
    model_caches: pml.ModelCaches | None = None,
) -> dict:
    glf = _glfactors_for(display_name, gamelog, profile)
    reg = regression_read(profile, glf)
    ctx = None
    player_id = _num(profile.get("pitcher_id") or profile.get("playerId") or profile.get("player_id"))
    player_id_i = int(player_id) if player_id is not None else None
    gl_slice = _gamelog_slice(gamelog, display_name)
    if is_today and matchup_row is not None:
        ctx = matchup_context(
            pitcher_name=display_name,
            pitcher_team=team,
            opp_team=opp,
            pitcher_hand=hand,
            is_home=is_home,
            matchup_row=matchup_row,
            lineups=lineups,
            weather=weather,
            split_cache=split_cache,
            season_fip=_num(profile.get("FIP")),
            pitch_mix_indexes=pitch_mix_indexes,
            player_id=player_id_i,
            model_caches=model_caches,
            profile=profile,
        )
        reg = apply_matchup_to_regression(reg, ctx, profile=profile)
    props = prop_projections(
        profile, reg, ctx,
        pitcher_name=display_name,
        team=team,
        opp=opp,
        hand=hand,
        is_home=is_home,
        is_today=is_today,
        glf=glf,
        gl_slice=gl_slice,
        pitch_mix_indexes=pitch_mix_indexes,
        player_id=player_id_i,
        model_caches=model_caches,
    )
    row = {
        "player_id": player_id_i,
        "name": display_name,
        "team": team,
        "opp": opp if is_today else "",
        "hand": hand,
        "today": is_today,
        "starts": int(_num(profile.get("starts")) or 0),
        **reg,
        "props": props,
    }
    if ctx and ctx.get("pitch_mix"):
        row["pitch_mix"] = ctx["pitch_mix"]
    return row


def _ip(v):
    s = str(v)
    if "." in s:
        w, f = s.split(".")
        try:
            return int(w) + int(f) / 3.0
        except ValueError:
            return _num(v) or 0.0
    return _num(v) or 0.0


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _effective_platoon_side(bats: str, pitcher_hand: str) -> str:
    """Switch hitters bat opposite the pitcher; L/R pass through."""
    b = str(bats or "R").strip().upper()[:1]
    ph = str(pitcher_hand or "R").strip().upper()[:1]
    if b == "S":
        return "L" if ph == "R" else "R"
    return b if b in ("L", "R") else "R"


def _lineup_platoon(lineups: pd.DataFrame | None, opp_team: str, pitcher_hand: str) -> dict:
    out = {"lhh": 0, "rhh": 0, "n": 0, "lhh_pct": None, "rhh_pct": None}
    if lineups is None or lineups.empty or not opp_team:
        return out
    sub = lineups[lineups["Team"].astype(str).str.upper().str.strip() == str(opp_team).upper()]
    if sub.empty:
        return out
    sides = [_effective_platoon_side(r.get("Bats"), pitcher_hand) for _, r in sub.iterrows()]
    out["lhh"] = sum(1 for s in sides if s == "L")
    out["rhh"] = sum(1 for s in sides if s == "R")
    out["n"] = out["lhh"] + out["rhh"]
    if out["n"]:
        out["lhh_pct"] = round(out["lhh"] / out["n"], 3)
        out["rhh_pct"] = round(out["rhh"] / out["n"], 3)
    return out


def _team_wrc_vs_hand(opp: str, pitcher_hand: str) -> float | None:
    fname = "metrics_vs_RHP.csv" if str(pitcher_hand).upper().startswith("R") else "metrics_vs_LHP.csv"
    df = load(fname)
    if df is None or "Tm" not in df.columns:
        return None
    row = df[df["Tm"].astype(str).str.upper().str.strip() == str(opp).upper()]
    if row.empty:
        return None
    return _num(row.iloc[0].get("wRC+"))


def _sp_split_fip(name: str, split: str, cache: dict) -> float | None:
    key = (name, split)
    if key in cache:
        return cache[key]
    fname = "sp_vs_LHH.csv" if split == "LHH" else "sp_vs_RHH.csv"
    df = load(fname)
    if df is None:
        cache[key] = None
        return None
    name_col = "Name" if "Name" in df.columns else "pitcher_name"
    nk = _norm_name(name)
    sub = df[df[name_col].astype(str).apply(_norm_name) == nk]
    val = _num(sub.iloc[0].get("FIP")) if not sub.empty else None
    cache[key] = val
    return val


def _weather_for_home(home_team: str, weather: pd.DataFrame | None) -> dict:
    if weather is None or weather.empty:
        return {}
    col = "home_team" if "home_team" in weather.columns else "Home"
    sub = weather[weather[col].astype(str).str.upper().str.strip() == str(home_team).upper()]
    if sub.empty:
        return {}
    w = sub.iloc[0]
    return {
        "temp_f": _num(w.get("temperature_f")),
        "wind_mph": _num(w.get("wind_speed_mph")),
        "wind_dir": str(w.get("wind_direction", "")).strip(),
        "conditions": str(w.get("conditions", "")).strip(),
        "dome": bool(w.get("is_dome", False)),
        "stadium": str(w.get("stadium_name", "")).strip(),
    }


def _build_pitch_mix_baselines(team_batting: pd.DataFrame | None) -> dict[str, dict]:
    """League-average xwOBA / whiff / CSW by normalized pitch_type."""
    if team_batting is None or team_batting.empty:
        return {}
    df = team_batting.copy()
    df["pitch_type"] = df["pitch_type"].map(_norm_pitch_type)
    df = df[~df["pitch_type"].isin(SKIP_PITCH_TYPES)]
    grp = df.groupby("pitch_type", as_index=False).agg(
        xwoba=("xwoba", "mean"),
        whiff_rate=("whiff_rate", "mean"),
        csw_rate=("csw_rate", "mean"),
        chase_rate=("chase_rate", "mean"),
    )
    return {
        str(r["pitch_type"]): {
            "xwoba": float(r["xwoba"]),
            "whiff_rate": float(r["whiff_rate"]),
            "csw_rate": float(r["csw_rate"]),
            "chase_rate": float(r["chase_rate"]),
        }
        for _, r in grp.iterrows()
    }


def _build_pitch_mix_indexes(
    pitcher_mix: pd.DataFrame | None,
    team_batting: pd.DataFrame | None,
) -> tuple[dict, dict, dict, dict[str, dict]]:
    """Indexes for pitcher rows + team batting rows + league baselines."""
    by_id: dict[int, pd.DataFrame] = {}
    by_name: dict[str, pd.DataFrame] = {}
    if pitcher_mix is not None and not pitcher_mix.empty:
        df = pitcher_mix.copy()
        df["pitch_type"] = df["pitch_type"].map(_norm_pitch_type)
        df = df[~df["pitch_type"].isin(SKIP_PITCH_TYPES)]
        for pid, sub in df.groupby("player_id"):
            pid_i = int(_num(pid) or 0)
            if pid_i:
                by_id[pid_i] = sub
        if "full_name" in df.columns:
            for nm, sub in df.groupby(df["full_name"].map(_registry_name_norm)):
                if nm:
                    by_name[nm] = sub
    team_index: dict[str, pd.DataFrame] = {}
    if team_batting is not None and not team_batting.empty:
        tdf = team_batting.copy()
        tdf["pitch_type"] = tdf["pitch_type"].map(_norm_pitch_type)
        tdf = tdf[~tdf["pitch_type"].isin(SKIP_PITCH_TYPES)]
        col = "team_abbr" if "team_abbr" in tdf.columns else "Tm"
        for tm, sub in tdf.groupby(tdf[col].astype(str).str.upper().str.strip()):
            if tm:
                team_index[tm] = sub
    baselines = _build_pitch_mix_baselines(team_batting)
    return by_id, by_name, team_index, baselines


def _pitch_mix_rows(
    indexes: tuple,
    *,
    pitcher_name: str,
    player_id: int | None,
) -> pd.DataFrame | None:
    by_id, by_name, _, _ = indexes
    if player_id and int(player_id) in by_id:
        return by_id[int(player_id)]
    nk = _norm_name(pitcher_name)
    return by_name.get(nk)


def _team_batting_rows(indexes: tuple, opp_team: str) -> pd.DataFrame | None:
    _, _, team_index, _ = indexes
    return team_index.get(str(opp_team).upper())


def pitch_mix_matchup(
    indexes: tuple,
    *,
    pitcher_name: str,
    opp_team: str,
    player_id: int | None = None,
) -> dict | None:
    """
    Pitch-mix matchup overlay (season or L14 CSVs from MLBMA).

    For each pitch type t with pitcher usage w_t (share of arsenal):
      contact_edge_t = (lg_xwoba_t - pit_xwoba_t) - (opp_xwoba_t - lg_xwoba_t)
        -> positive when pitcher suppresses contact AND opponent is weak vs t
      whiff_edge_t   = (pit_whiff_t - lg_whiff_t) - (opp_whiff_t - lg_whiff_t)
        -> positive when pitcher's out-pitch beats opponent's bat-to-ball on t
      csw_edge_t     = (pit_csw_t - lg_csw_t) / 100
      pitch_score_t  = w_t * (0.40*whiff_edge + 0.35*contact_edge + 0.25*csw_edge)

    matchup_score = sum(pitch_score_t). Prop nudges scale score into K%/BB%/ER/Outs
    alongside existing platoon overlays (not replacing them).
    """
    if not opp_team:
        return None
    pit_rows = _pitch_mix_rows(indexes, pitcher_name=pitcher_name, player_id=player_id)
    opp_rows = _team_batting_rows(indexes, opp_team)
    _, _, _, baselines = indexes
    if pit_rows is None or opp_rows is None or not baselines:
        return None

    opp_by_type = {str(r["pitch_type"]): r for _, r in opp_rows.iterrows()}
    pitch_scores: list[tuple[str, float, float, str]] = []
    total_score = 0.0
    usage_total = 0.0

    for _, pr in pit_rows.iterrows():
        pt = str(pr["pitch_type"])
        usage = _num(pr.get("pitch_pct")) or 0.0
        if usage < PITCH_MIX_MIN_USAGE or pt not in baselines:
            continue
        opp = opp_by_type.get(pt)
        if opp is None:
            continue
        lg = baselines[pt]
        w = usage / 100.0
        usage_total += w

        pit_xw = _num(pr.get("xwoba")) or lg["xwoba"]
        opp_xw = _num(opp.get("xwoba")) or lg["xwoba"]
        pit_whiff = _num(pr.get("whiff_rate")) or lg["whiff_rate"]
        opp_whiff = _num(opp.get("whiff_rate")) or lg["whiff_rate"]
        pit_csw = _num(pr.get("csw_rate")) or lg["csw_rate"]

        contact_edge = (lg["xwoba"] - pit_xw) - (opp_xw - lg["xwoba"])
        whiff_edge = (pit_whiff - lg["whiff_rate"] - (opp_whiff - lg["whiff_rate"])) / 100.0
        csw_edge = (pit_csw - lg["csw_rate"]) / 100.0
        pitch_score = w * (0.40 * whiff_edge + 0.35 * contact_edge + 0.25 * csw_edge)
        total_score += pitch_score

        pname = str(pr.get("pitch_name") or pt)
        if contact_edge > 0.04 and whiff_edge > 0:
            tag = "favorable"
        elif contact_edge < -0.04 or whiff_edge < -0.02:
            tag = "hostile"
        else:
            tag = "neutral"
        pitch_scores.append((pt, pitch_score, usage, f"{pname} {usage:.0f}% ({tag})"))

    if usage_total < 0.25 or not pitch_scores:
        return None

    pitch_scores.sort(key=lambda x: abs(x[1]), reverse=True)
    top = pitch_scores[:3]
    heavy = max(pitch_scores, key=lambda x: x[2])
    best = max(pitch_scores, key=lambda x: x[1])
    worst = min(pitch_scores, key=lambda x: x[1])

    if total_score >= 0.012:
        tone = "pitch mix favors SP — whiff/CSW edges align with opp weaknesses"
    elif total_score <= -0.012:
        tone = "opp mashes SP's primary pitches — contact risk elevated"
    else:
        tone = "neutral pitch-mix overlap"

    note_parts = [f"{heavy[3].split(' (')[0]}-heavy"]
    if best[1] > 0.003:
        note_parts.append(f"best vs {best[0]}")
    if worst[1] < -0.003:
        note_parts.append(f"watch {worst[0]}")
    note = " · ".join(note_parts) + f" — {tone}"

    return {
        "score": round(total_score, 3),
        "k_pct_delta": round(total_score * PITCH_MIX_K_SCALE, 2),
        "bb_pct_delta": round(-total_score * PITCH_MIX_BB_SCALE, 2),
        "er_factor": round(1.0 - total_score * PITCH_MIX_ER_SCALE, 3),
        "outs_factor": round(1.0 + total_score * PITCH_MIX_OUTS_SCALE, 3),
        "top_pitches": top,
        "note": note,
    }


def _verdict_from_luck(luck: float) -> tuple[str, str]:
    if luck >= VERDICT_THRESHOLD:
        return "REGRESSION", "fade"
    if luck <= -VERDICT_THRESHOLD:
        return "PROGRESSION", "back"
    return "STABLE", "neutral"


def matchup_context(
    *,
    pitcher_name: str,
    pitcher_team: str,
    opp_team: str,
    pitcher_hand: str,
    is_home: bool,
    matchup_row: dict | None,
    lineups: pd.DataFrame | None,
    weather: pd.DataFrame | None,
    split_cache: dict,
    season_fip: float | None,
    pitch_mix_indexes: tuple | None = None,
    player_id: int | None = None,
    model_caches: pml.ModelCaches | None = None,
    profile: dict | None = None,
) -> dict:
    """Today's slate context — platoon/split FIP/park/weather/pitch-mix on luck; offense on ER channel."""
    factors: list[tuple] = []
    adj = 0.0
    ph = str(pitcher_hand or "R").upper()[:1]
    home_team = pitcher_team if is_home else opp_team
    plat = _lineup_platoon(lineups, opp_team, ph)

    if plat["n"] >= 7:
        if ph == "R":
            share = plat["lhh_pct"] or LG_LHH_SHARE
            pressure = (share - LG_LHH_SHARE) * 2.4
            note = (f"{plat['lhh']}/{plat['n']} LHH ({share * 100:.0f}%) — "
                    f"{'platoon stress on RHP' if pressure > 0.05 else 'same-side heavy, RHP-friendly' if pressure < -0.05 else 'neutral platoon mix'}")
        else:
            share = plat["rhh_pct"] or LG_LHH_SHARE
            pressure = (share - LG_LHH_SHARE) * 2.4
            note = (f"{plat['rhh']}/{plat['n']} RHH ({share * 100:.0f}%) — "
                    f"{'platoon stress on LHP' if pressure > 0.05 else 'same-side heavy, LHP-friendly' if pressure < -0.05 else 'neutral platoon mix'}")
        adj += pressure * MATCHUP_W_PLATOON
        factors.append(_factor("platoon", "Lineup platoon mix", round(pressure, 2), note))

    opp_osi = None
    lineup_proj_osi = None
    team_wrc = _team_wrc_vs_hand(opp_team, ph)
    if matchup_row:
        if str(pitcher_team).upper() == str(matchup_row.get("Home", "")).upper():
            opp_osi = _num(matchup_row.get("Away_OSI"))
        elif str(pitcher_team).upper() == str(matchup_row.get("Away", "")).upper():
            opp_osi = _num(matchup_row.get("Home_OSI"))
    if model_caches and opp_osi is not None:
        lineup_proj_osi, lpo_factors = pml.lineup_weighted_proj_osi(
            lineups, opp_team, ph, model_caches,
        )
        factors.extend(lpo_factors)
    if opp_osi is not None:
        display_osi = lineup_proj_osi if lineup_proj_osi is not None else opp_osi
        factors.append(_factor(
            "opp_osi", "Opponent lineup OSI", round(display_osi, 1),
            "lineup-weighted projOSI" if lineup_proj_osi else "matchup OSI (ER channel)",
        ))

    fip_l = _sp_split_fip(pitcher_name, "LHH", split_cache)
    fip_r = _sp_split_fip(pitcher_name, "RHH", split_cache)
    if fip_l is not None and fip_r is not None and plat["lhh_pct"] is not None and season_fip is not None:
        l_share = plat["lhh_pct"]
        exp_fip = l_share * fip_l + (1 - l_share) * fip_r
        split_delta = exp_fip - season_fip
        adj += split_delta * MATCHUP_W_SPLIT_FIP
        factors.append(_factor(
            "split_fip", "Split FIP vs lineup", round(exp_fip, 2),
            f"weighted vs today's {l_share * 100:.0f}% LHH mix (Δ {split_delta:+.2f} vs season FIP)",
        ))

    park = config.park_factor_for_team(home_team)
    park_push = (park - 1.0) * 1.15
    adj += park_push * MATCHUP_W_PARK
    if park >= 1.06:
        park_note = " hitter-friendly — run environment amplifies regression"
    elif park <= 0.94:
        park_note = " suppresses offense — progression harder to sustain"
    else:
        park_note = " neutral run environment"
    factors.append(_factor("park", "Ballpark factor", round(park, 2), park_note))

    wx = _weather_for_home(home_team, weather)
    if wx:
        wx_push = 0.0
        wx_bits = []
        if wx.get("dome"):
            wx_bits.append("dome — weather neutral")
        else:
            wind = wx.get("wind_mph")
            temp = wx.get("temp_f")
            if wind is not None and wind >= 12:
                wx_push += 0.18
                wx_bits.append(f"wind {wind:.0f} mph {wx.get('wind_dir', '')} — fly-ball/total risk")
            elif wind is not None and wind >= 8:
                wx_push += 0.08
                wx_bits.append(f"breeze {wind:.0f} mph {wx.get('wind_dir', '')}")
            if temp is not None and temp <= 50:
                wx_push -= 0.14
                wx_bits.append(f"cold {temp:.0f}°F — suppresses offense")
            elif temp is not None and temp >= 82:
                wx_push += 0.06
                wx_bits.append(f"warm {temp:.0f}°F")
            if wx.get("conditions"):
                wx_bits.append(str(wx["conditions"]))
        adj += wx_push
        if wx_bits:
            factors.append(_factor("weather", "Weather / environment", round(wx_push, 2), " · ".join(wx_bits)))

    pitch_mix = None
    if pitch_mix_indexes:
        pitch_mix = pitch_mix_matchup(
            pitch_mix_indexes,
            pitcher_name=pitcher_name,
            opp_team=opp_team,
            player_id=player_id,
        )
        if pitch_mix:
            # +score favors SP → subtract from luck adj (same convention as other matchup layers:
            # positive adj = harder outing / regression pressure).
            pm_push = pitch_mix["score"] * PITCH_MIX_LUCK_SCALE
            adj -= pm_push * PITCH_MIX_MATCHUP_W
            factors.append(_factor("pitch_mix", "Pitch mix matchup", pitch_mix["score"], pitch_mix["note"]))

    tier_fip, ps_fip = None, None
    if model_caches and profile is not None:
        tier_fip, tier_factors = pml.osi_tier_skill(
            profile, model_caches, opp_osi, pitcher_name,
        )
        factors.extend(tier_factors)
        ps_fip, ps_factors = pml.pitchscore_anchor(matchup_row, pitcher_team, is_home)
        factors.extend(ps_factors)

    er_offense_push = 0.0
    if model_caches:
        er_offense_push, er_factors = pml.offense_er_push(lineup_proj_osi, opp_osi, team_wrc)
        factors.extend(er_factors)

    return {
        "adj": round(adj, 2),
        "factors": factors,
        "opp_osi": opp_osi,
        "lineup_proj_osi": lineup_proj_osi,
        "team_wrc": team_wrc,
        "er_offense_push": er_offense_push,
        "tier_fip": tier_fip if model_caches else None,
        "pitchscore_fip": ps_fip if model_caches else None,
        "platoon": plat,
        "park": park,
        "home_team": home_team,
        "weather": wx,
        "pitch_mix": pitch_mix,
    }


def apply_matchup_to_regression(reg: dict, ctx: dict | None, *, profile: dict | None = None) -> dict:
    """Blend today's matchup context into luck score + verdict; preserve base luck."""
    reg = dict(reg)
    base_luck = reg.get("luck", 0.0)
    reg["luck_base"] = base_luck
    if not ctx or not ctx.get("factors"):
        reg["matchup_adj"] = 0.0
        return reg
    adj = ctx.get("adj") or 0.0
    combined = round(base_luck + adj, 2)
    reg["matchup_adj"] = adj
    reg["luck"] = combined
    reg["factors"] = list(reg.get("factors") or []) + list(ctx.get("factors") or [])
    verdict, tone = _verdict_from_luck(combined)
    reg["verdict"] = verdict
    reg["tone"] = tone
    if ctx.get("opp_osi") is not None:
        reg["opp_osi"] = ctx["opp_osi"]
    if ctx.get("lineup_proj_osi") is not None:
        reg["lineup_proj_osi"] = ctx["lineup_proj_osi"]
    if ctx.get("platoon"):
        reg["lineup_platoon"] = ctx["platoon"]
    if ctx.get("park") is not None:
        reg["park_factor"] = ctx["park"]
    if ctx.get("er_offense_push") is not None:
        reg["er_offense_push"] = ctx["er_offense_push"]
    skill = reg.get("skill_era")
    tier_fip = ctx.get("tier_fip")
    if tier_fip is not None and skill is not None:
        skill = round(skill * (1 - OSI_TIER_SKILL_BLEND) + tier_fip * OSI_TIER_SKILL_BLEND, 2)
    ps_fip = ctx.get("pitchscore_fip")
    if ps_fip is not None and skill is not None:
        skill = round(skill * (1 - PITCHSCORE_SKILL_BLEND) + ps_fip * PITCHSCORE_SKILL_BLEND, 2)
    for f in ctx.get("factors") or []:
        if f.get("key") == "split_fip" and f.get("value") is not None:
            try:
                skill = round(float(f["value"]), 2)
            except (TypeError, ValueError):
                pass
            break
    if skill is not None:
        reg["skill_era"] = skill
    return reg


def _gamelog_factors(gl: pd.DataFrame) -> dict:
    """Luck/trend/fatigue factors for one pitcher's game log."""
    out = {"babip": None, "lob": None, "k_trend": None, "bb_trend": None,
           "gs_trend": None, "fatigue": None, "starts": len(gl)}
    if gl.empty:
        return out
    s = {c: pd.to_numeric(gl.get(c), errors="coerce") for c in ("H", "BB", "HR", "K", "R", "batters_faced", "pitches", "game_score")}
    H, BB, HR, K, R = (s[c].sum() for c in ("H", "BB", "HR", "K", "R"))
    bf = s["batters_faced"].sum()
    bip = bf - K - HR - BB
    if bip > 0:
        out["babip"] = round((H - HR) / bip, 3)
    lob_den = (H + BB - 1.4 * HR)
    if lob_den > 0:
        out["lob"] = round((H + BB - R) / lob_den, 3)

    g = gl.copy()
    if "date" in g.columns:
        g = g.sort_values("date")
    recent = g.tail(RECENT_N)

    def rate(df, col):
        v = pd.to_numeric(df.get(col), errors="coerce")
        b = pd.to_numeric(df.get("batters_faced"), errors="coerce")
        return (v.sum() / b.sum() * 100) if b.sum() > 0 else None

    k_r, k_s = rate(recent, "K"), rate(g, "K")
    bb_r, bb_s = rate(recent, "BB"), rate(g, "BB")
    out["k_trend"] = round(k_r - k_s, 1) if (k_r is not None and k_s is not None) else None
    out["bb_trend"] = round(bb_r - bb_s, 1) if (bb_r is not None and bb_s is not None) else None
    gs_r = pd.to_numeric(recent.get("game_score"), errors="coerce").mean()
    gs_s = pd.to_numeric(g.get("game_score"), errors="coerce").mean()
    out["gs_trend"] = round(gs_r - gs_s, 1) if pd.notna(gs_r) and pd.notna(gs_s) else None
    rp = pd.to_numeric(recent.get("pitches"), errors="coerce").mean()
    out["fatigue"] = round(float(rp), 0) if pd.notna(rp) else None
    return out


def regression_read(p: dict, gl_factors: dict) -> dict:
    """Composite luck (expected ERA change, in runs) + verdict + factor breakdown."""
    era, fip, xfip = _num(p.get("ERA")), _num(p.get("FIP")), _num(p.get("xFIP"))
    oor = _num(p.get("OOR_faced"))
    factors = []
    luck = 0.0  # positive = lucky now -> REGRESSION (results worse going forward)

    if fip is not None and era is not None:
        d = fip - era; luck += 0.35 * d
        factors.append(_factor("era_fip", "ERA vs FIP", round(d, 2),
                               "ERA below FIP = lucky" if d > 0 else "ERA above FIP = unlucky"))
    if xfip is not None and era is not None:
        d = xfip - era; luck += 0.20 * d
        factors.append(_factor("era_xfip", "ERA vs xFIP", round(d, 2), ""))
    if fip is not None and xfip is not None:
        d = xfip - fip; luck += 0.10 * d
        factors.append(_factor("hr_suppression", "HR suppression (xFIP-FIP)", round(d, 2),
                               "homer-lucky" if d > 0 else "homer-unlucky"))
    if gl_factors.get("babip") is not None:
        d = (LG_BABIP - gl_factors["babip"]) * 12
        luck += 0.18 * d
        factors.append(_factor("babip", "BABIP", gl_factors["babip"],
                               "low = lucky on contact" if gl_factors["babip"] < LG_BABIP else "high = unlucky"))
    if gl_factors.get("lob") is not None:
        d = (gl_factors["lob"] - LG_LOB) * 6
        luck += 0.12 * d
        factors.append(_factor("lob", "LOB% (strand)", gl_factors["lob"],
                               "high = lucky sequencing" if gl_factors["lob"] > LG_LOB else "low = unlucky"))
    if oor is not None:
        d = (LG_OOR - oor) * 0.05
        luck += 0.05 * d
        factors.append(_factor("schedule_oor", "Schedule faced (OOR)", round(oor, 1),
                               "soft slate inflated results" if oor < LG_OOR else "tough slate"))

    # skill trend modifies conviction (real change, not luck)
    if gl_factors.get("k_trend") is not None:
        factors.append(_factor("k_trend", "K% trend (L3 vs szn)", gl_factors["k_trend"], ""))
    if gl_factors.get("bb_trend") is not None:
        factors.append(_factor("bb_trend", "BB% trend", gl_factors["bb_trend"], ""))
    if gl_factors.get("fatigue") is not None:
        factors.append(_factor("pitch_count", "Recent pitch count", gl_factors["fatigue"],
                               "heavy" if gl_factors["fatigue"] >= 100 else ""))

    luck = round(luck, 2)
    verdict, tone = _verdict_from_luck(luck)
    skill_era = round((fip * 0.55 + xfip * 0.45) if (fip is not None and xfip is not None)
                      else (fip if fip is not None else era or 4.10), 2)
    return {"verdict": verdict, "tone": tone, "luck": luck, "luck_base": luck, "matchup_adj": 0.0,
            "skill_era": skill_era, "era": era, "fip": fip, "xfip": xfip, "factors": factors}


def prop_projections(
    p: dict,
    reg: dict,
    ctx: dict | None = None,
    *,
    pitcher_name: str = "",
    team: str = "",
    opp: str = "",
    hand: str = "R",
    is_home: bool = False,
    is_today: bool = False,
    glf: dict | None = None,
    gl_slice: pd.DataFrame | None = None,
    pitch_mix_indexes: tuple | None = None,
    player_id: int | None = None,
    model_caches: pml.ModelCaches | None = None,
) -> dict:
    """Regression-adjusted projections + leans. K/BB vs ER channels separated (anti double-count)."""
    extra_factors: list[dict] = []
    avg_ip = _num(p.get("avg_IP")) or 5.3
    caches = model_caches or pml.ModelCaches()

    l14, l14_factors = pml.l14_blend(p, caches, pitcher_name or str(p.get("pitcher_name", "")))
    extra_factors.extend(l14_factors)
    k_pct = l14.get("k_pct") or _num(p.get("K_pct"))
    bb_pct = l14.get("bb_pct") or _num(p.get("BB_pct"))
    if k_pct is not None and k_pct <= 1.5:
        k_pct *= 100
    if bb_pct is not None and bb_pct <= 1.5:
        bb_pct *= 100

    glf = glf or {}
    if glf.get("k_trend") is not None:
        k_pct = (k_pct or 21) + glf["k_trend"] * 0.35
    if glf.get("bb_trend") is not None:
        bb_pct = (bb_pct or 8) + glf["bb_trend"] * 0.35

    pm = ctx.get("pitch_mix") if ctx else None
    er_factor = 1.0
    outs_factor = 1.0
    if pm:
        k_pct = (k_pct or 21) + pm.get("k_pct_delta", 0)
        bb_pct = (bb_pct or 8) + pm.get("bb_pct_delta", 0)

    if is_today and opp and pitch_mix_indexes:
        pit_rows = _pitch_mix_rows(pitch_mix_indexes, pitcher_name=pitcher_name, player_id=player_id)
        lk, lb, lpm_f = pml.lineup_pitch_mix_kbb(
            load("today_lineups.csv"), opp, hand, caches, pit_rows,
        )
        k_pct = (k_pct or 21) + lk
        bb_pct = (bb_pct or 8) + lb
        extra_factors.extend(lpm_f)

    sk, sb, sav_f = pml.savant_kbb_modifier(opp, caches)
    k_pct = (k_pct or 21) + sk
    bb_pct = (bb_pct or 8) + sb
    extra_factors.extend(sav_f)

    hook_factor, hook_f = pml.bullpen_hook_factor(team, caches)
    outs_factor *= hook_factor
    extra_factors.extend(hook_f)

    if gl_slice is not None:
        _, rest_outs, rest_k, rest_f = pml.rest_workload(gl_slice, p)
        outs_factor *= rest_outs
        k_pct = (k_pct or 21) + rest_k
        extra_factors.extend(rest_f)

    signal_boost = {"K": 0.0, "BB": 0.0}
    if is_today and opp:
        signal_boost, sig_f = pml.signal_conviction(team, opp, is_home, caches)
        extra_factors.extend(sig_f)

    if ctx:
        roll_push, roll_f = pml.rolling_opp_osi_factor(
            pitcher_name, ctx.get("opp_osi"), caches,
        )
        er_factor += roll_push
        extra_factors.extend(roll_f)
        l10_push, l10_f = pml.team_l10_hand_check(opp, hand, caches)
        er_factor += l10_push
        extra_factors.extend(l10_f)

    bf = avg_ip * 4.25
    outs = round(avg_ip * 3 * outs_factor, 1)
    k_proj = round(bf * (k_pct or 21) / 100, 1)
    bb_proj = round(bf * (bb_pct or 8) / 100, 1)

    blended = reg["skill_era"] * 0.6 + (reg.get("era") or reg["skill_era"]) * 0.4
    park = ctx.get("park") if ctx else None
    if park is not None:
        blended *= 0.85 + park * 0.15
    if ctx and ctx.get("er_offense_push"):
        blended += ctx["er_offense_push"] * config.SP_FIP_WEIGHT
    er_proj = round(blended / 9 * avg_ip * er_factor, 1)

    f5_props, f5_f = pml.f5_projection(p, reg["skill_era"], er_factor)
    extra_factors.extend(f5_f)

    def lean(verdict, stat, boost=0.0):
        base = "—"
        if verdict == "REGRESSION":
            base = {"K": "under", "Outs": "under", "ER": "over", "BB": "over", "F5": "over"}[stat]
        elif verdict == "PROGRESSION":
            base = {"K": "over", "Outs": "over", "ER": "under", "BB": "under", "F5": "under"}[stat]
        if boost >= 0.25 and base in ("over", "under"):
            return base
        if boost >= 0.15 and base != "—":
            return base
        return base

    v = reg["verdict"]
    if extra_factors:
        reg["factors"] = list(reg.get("factors") or []) + extra_factors

    return {
        "K": {"proj": k_proj, "lean": lean(v, "K", signal_boost["K"]), "conviction": round(signal_boost["K"], 2)},
        "BB": {"proj": bb_proj, "lean": lean(v, "BB", signal_boost["BB"]), "conviction": round(signal_boost["BB"], 2)},
        "Outs": {"proj": outs, "lean": lean(v, "Outs")},
        "ER": {"proj": er_proj, "lean": lean(v, "ER")},
        "F5": f5_props,
        "model_version": config.MODEL_VERSION,
    }


def pitcher_board() -> list[dict]:
    check_slate_freshness("the boards")
    profiles = load("sp_profiles.csv")
    standard = load("sp_standard.csv")
    gamelog = load("sp_gamelog.csv")
    matchups = load("today_matchups.csv")
    lineups = load("today_lineups.csv")
    weather = load("today_weather.csv")
    if profiles is None and standard is None:
        return []

    index = _build_pitcher_index(profiles, standard)
    today_sp = _parse_today_starters(matchups)
    split_cache: dict = {}
    pitcher_mix = load_pitch_mix("pitcher")
    team_batting_mix = load_pitch_mix("team_batting")
    pitch_mix_indexes = _build_pitch_mix_indexes(pitcher_mix, team_batting_mix)
    model_caches = pml.build_caches(gamelog)
    rows = []
    seen_today: set[str] = set()

    if profiles is not None and not profiles.empty:
        profiles = profiles.copy()
        profiles["__name"] = profiles["pitcher_name"].astype(str).str.strip()
        for _, p in profiles.iterrows():
            display = p["__name"]
            nk = _norm_name(display)
            is_today = nk in today_sp
            if is_today:
                display, team, opp, hand, is_home, mrow = today_sp[nk]
                seen_today.add(nk)
            else:
                team = str(p.get("pitcher_team", "")).upper()
                opp, hand, is_home, mrow = "", str(p.get("pitcher_hand", "R")).upper()[:1], False, None
            rows.append(_pitcher_row(
                p.to_dict(), display_name=display, team=team, opp=opp, hand=hand,
                is_today=is_today, gamelog=gamelog, lineups=lineups, weather=weather,
                split_cache=split_cache, matchup_row=mrow, is_home=is_home,
                pitch_mix_indexes=pitch_mix_indexes, model_caches=model_caches,
            ))

    # Slate starters missing from sp_profiles (common for arms without gamelog rows yet)
    for nk, (display, team, opp, hand, is_home, mrow) in today_sp.items():
        if nk in seen_today:
            continue
        profile = _resolve_pitcher(display, team, hand, index)
        if profile is None:
            profile = {
                "pitcher_name": display, "pitcher_team": team, "pitcher_hand": hand,
                "starts": 0, "avg_IP": 5.3, "ERA": None, "FIP": None, "xFIP": None,
                "K_pct": 21.0, "BB_pct": 8.0,
            }
        rows.append(_pitcher_row(
            profile, display_name=display, team=team, opp=opp, hand=hand,
            is_today=True, gamelog=gamelog, lineups=lineups, weather=weather,
            split_cache=split_cache, matchup_row=mrow, is_home=is_home,
            pitch_mix_indexes=pitch_mix_indexes, model_caches=model_caches,
        ))

    rows.sort(key=lambda r: (not r["today"], -abs(r["luck"])))
    return rows


def market_board() -> list[dict]:
    check_slate_freshness("the boards")
    matchups = load("today_matchups.csv")
    profiles = load("sp_profiles.csv")
    standard = load("sp_standard.csv")
    teams = load("team_profiles.csv")
    lineups = load("today_lineups.csv")
    weather = load("today_weather.csv")
    if matchups is None:
        return []
    index = _build_pitcher_index(profiles, standard)
    bp_by_team = {}
    if teams is not None:
        for _, t in teams.iterrows():
            bp_by_team[str(t.get("team", "")).upper()] = t.to_dict()

    glf_cache = {}
    split_cache = {}
    gamelog = load("sp_gamelog.csv")
    pitcher_mix = load_pitch_mix("pitcher")
    team_batting_mix = load_pitch_mix("team_batting")
    pitch_mix_indexes = _build_pitch_mix_indexes(pitcher_mix, team_batting_mix)
    model_caches = pml.build_caches(gamelog)

    def reg_for(name, *, team=None, opp=None, hand=None, is_home=None, matchup_row=None):
        p = _resolve_pitcher(str(name).strip(), team, hand, index)
        if not p:
            return None
        nm = str(name).strip()
        if nm not in glf_cache:
            glf_cache[nm] = _glfactors_for(nm, gamelog, p)
        reg = regression_read(p, glf_cache[nm])
        if team and opp and hand is not None and matchup_row is not None:
            pid = _num(p.get("pitcher_id") or p.get("playerId") or p.get("player_id"))
            ctx = matchup_context(
                pitcher_name=nm,
                pitcher_team=team,
                opp_team=opp,
                pitcher_hand=hand,
                is_home=bool(is_home),
                matchup_row=matchup_row,
                lineups=lineups,
                weather=weather,
                split_cache=split_cache,
                season_fip=_num(p.get("FIP")),
                pitch_mix_indexes=pitch_mix_indexes,
                player_id=int(pid) if pid is not None else None,
                model_caches=model_caches,
                profile=p,
            )
            reg = apply_matchup_to_regression(reg, ctx, profile=p)
        player_id = _num(p.get("pitcher_id") or p.get("playerId") or p.get("player_id"))
        reg["player_id"] = int(player_id) if player_id is not None else None
        return reg

    def bp_fatigue(team):
        t = bp_by_team.get(str(team).upper())
        if not t:
            return None
        hilev = _num(t.get("bullpen_high_lev_era"))
        ir = _num(t.get("bullpen_ir_scored_pct"))
        era = _num(t.get("bullpen_era"))
        score = 0.0
        if hilev is not None:
            score += (hilev - 4.0) * 0.5
        if ir is not None:
            score += (ir - 33) * 0.03
        if era is not None:
            score += (era - 4.0) * 0.4
        return {"score": round(score, 2), "hilev": hilev, "ir": ir, "era": era}

    rows = []
    for _, g in matchups.iterrows():
        a, h = str(g.get("Away", "")).upper(), str(g.get("Home", "")).upper()
        if not a or not h:
            continue
        a_reg = reg_for(g.get("Away_SP"), team=a, opp=h, hand=str(g.get("Away_Hand", "R")).upper()[:1],
                        is_home=False, matchup_row=g.to_dict())
        h_reg = reg_for(g.get("Home_SP"), team=h, opp=a, hand=str(g.get("Home_Hand", "R")).upper()[:1],
                        is_home=True, matchup_row=g.to_dict())
        a_bp, h_bp = bp_fatigue(a), bp_fatigue(h)
        a_osi, h_osi = _num(g.get("Away_OSI")), _num(g.get("Home_OSI"))
        drivers = []

        # ML lean: better (lower) skill_era + offense edge + opponent bullpen weakness
        score = 0.0  # positive favors HOME
        if a_reg and h_reg:
            score += (a_reg["skill_era"] - h_reg["skill_era"]) * 0.6
            if a_reg["verdict"] == "REGRESSION": score += 0.3; drivers.append(f"{a} SP regression risk")
            if h_reg["verdict"] == "REGRESSION": score -= 0.3; drivers.append(f"{h} SP regression risk")
            if a_reg["verdict"] == "PROGRESSION": score -= 0.2
            if h_reg["verdict"] == "PROGRESSION": score += 0.2
        if a_osi is not None and h_osi is not None:
            score += (h_osi - a_osi) * 0.03
        if a_bp and h_bp:
            score += (a_bp["score"] - h_bp["score"]) * 0.2
            if a_bp["score"] > 1.0: drivers.append(f"{a} bullpen fatigued/weak")
            if h_bp["score"] > 1.0: drivers.append(f"{h} bullpen fatigued/weak")
        ml_lean = h if score > 0.25 else a if score < -0.25 else "—"

        # Total lean: weak SP + tired pens + strong offenses -> over
        tot = 0.0
        for reg in (a_reg, h_reg):
            if reg and reg["verdict"] == "REGRESSION": tot += 0.4
            if reg and reg["verdict"] == "PROGRESSION": tot -= 0.4
            if reg and reg["skill_era"]: tot += (reg["skill_era"] - 4.10) * 0.3
        for bp in (a_bp, h_bp):
            if bp and bp["score"] > 1.0: tot += 0.3
        for osi in (a_osi, h_osi):
            if osi is not None: tot += (osi - 50) * 0.02
        total_lean = "OVER" if tot > 0.6 else "UNDER" if tot < -0.6 else "—"

        # F5: starter-only (no bullpen)
        f5 = 0.0
        for reg in (a_reg, h_reg):
            if reg and reg["skill_era"]: f5 += (reg["skill_era"] - 4.10) * 0.4
            if reg and reg["verdict"] == "REGRESSION": f5 += 0.3
        f5_lean = "OVER" if f5 > 0.5 else "UNDER" if f5 < -0.5 else "—"

        rows.append({
            "game": f"{a}@{h}", "away": a, "home": h, "time": str(g.get("Time", "")),
            "away_sp": str(g.get("Away_SP", "")), "home_sp": str(g.get("Home_SP", "")),
            "away_sp_id": a_reg.get("player_id") if a_reg else None,
            "home_sp_id": h_reg.get("player_id") if h_reg else None,
            "away_skill": a_reg["skill_era"] if a_reg else None, "home_skill": h_reg["skill_era"] if h_reg else None,
            "away_verdict": a_reg["verdict"] if a_reg else None, "home_verdict": h_reg["verdict"] if h_reg else None,
            "away_bp": a_bp["score"] if a_bp else None, "home_bp": h_bp["score"] if h_bp else None,
            "ml_lean": ml_lean, "total_lean": total_lean, "f5_lean": f5_lean,
            "drivers": drivers[:4],
        })
    return rows
