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
from _compat import check_slate_freshness, load

# League baselines
LG_BABIP, LG_LOB, LG_HR9, LG_OOR, LG_ERA = 0.295, 0.72, 1.15, 44.0, 4.10
LG_LHH_SHARE = 0.42  # typical share of LHH in a lineup vs RHP
RECENT_N = 3
VERDICT_THRESHOLD = 0.6


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
) -> dict:
    glf = _glfactors_for(display_name, gamelog, profile)
    reg = regression_read(profile, glf)
    ctx = None
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
        )
        reg = apply_matchup_to_regression(reg, ctx)
    props = prop_projections(profile, reg, ctx)
    player_id = _num(profile.get("pitcher_id") or profile.get("playerId") or profile.get("player_id"))
    return {
        "player_id": int(player_id) if player_id is not None else None,
        "name": display_name,
        "team": team,
        "opp": opp if is_today else "",
        "hand": hand,
        "today": is_today,
        "starts": int(_num(profile.get("starts")) or 0),
        **reg,
        "props": props,
    }


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
) -> dict:
    """Today's slate context layered on top of season luck — platoon, OSI, park, weather, splits."""
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
        adj += pressure * 0.38
        factors.append(("Lineup platoon mix", round(pressure, 2), note))

    opp_osi = None
    if matchup_row:
        if str(pitcher_team).upper() == str(matchup_row.get("Home", "")).upper():
            opp_osi = _num(matchup_row.get("Away_OSI"))
        elif str(pitcher_team).upper() == str(matchup_row.get("Away", "")).upper():
            opp_osi = _num(matchup_row.get("Home_OSI"))
    if opp_osi is not None:
        osi_push = (opp_osi - 50) * 0.035
        adj += osi_push * 0.30
        if opp_osi >= 58:
            osi_note = "hot split-adjusted lineup — lucky ERAs bleed fast"
        elif opp_osi <= 42:
            osi_note = "cold lineup — regression damage may stay hidden"
        else:
            osi_note = "league-average lineup OSI vs this SP hand"
        factors.append(("Opponent lineup OSI", round(opp_osi, 1), osi_note))

    wrc = _team_wrc_vs_hand(opp_team, ph)
    if wrc is not None:
        wrc_push = (wrc - 100) * 0.012
        adj += wrc_push * 0.18
        factors.append(("Opp wRC+ vs hand", round(wrc, 0),
                        "offense hits this hand" if wrc >= 108 else "offense weak vs this hand" if wrc <= 92 else ""))

    fip_l = _sp_split_fip(pitcher_name, "LHH", split_cache)
    fip_r = _sp_split_fip(pitcher_name, "RHH", split_cache)
    if fip_l is not None and fip_r is not None and plat["lhh_pct"] is not None and season_fip is not None:
        l_share = plat["lhh_pct"]
        exp_fip = l_share * fip_l + (1 - l_share) * fip_r
        split_delta = exp_fip - season_fip
        adj += split_delta * 0.22
        factors.append(("Split FIP vs lineup", round(exp_fip, 2),
                        f"weighted vs today's {l_share * 100:.0f}% LHH mix (Δ {split_delta:+.2f} vs season FIP)"))

    park = config.park_factor_for_team(home_team)
    park_push = (park - 1.0) * 1.15
    adj += park_push * 0.22
    if park >= 1.06:
        park_note = " hitter-friendly — run environment amplifies regression"
    elif park <= 0.94:
        park_note = " suppresses offense — progression harder to sustain"
    else:
        park_note = " neutral run environment"
    factors.append(("Ballpark factor", round(park, 2), park_note))

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
            factors.append(("Weather / environment", round(wx_push, 2), " · ".join(wx_bits)))

    return {
        "adj": round(adj, 2),
        "factors": factors,
        "opp_osi": opp_osi,
        "platoon": plat,
        "park": park,
        "home_team": home_team,
        "weather": wx,
    }


def apply_matchup_to_regression(reg: dict, ctx: dict | None) -> dict:
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
    if ctx.get("platoon"):
        reg["lineup_platoon"] = ctx["platoon"]
    if ctx.get("park") is not None:
        reg["park_factor"] = ctx["park"]
    # Nudge skill ERA toward split-weighted expectation when available
    for label, val, _note in ctx.get("factors") or []:
        if str(label).startswith("Split FIP") and val is not None:
            try:
                reg["skill_era"] = round(float(val), 2)
            except (TypeError, ValueError):
                pass
            break
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
        factors.append(("ERA vs FIP", round(d, 2), "ERA below FIP = lucky" if d > 0 else "ERA above FIP = unlucky"))
    if xfip is not None and era is not None:
        d = xfip - era; luck += 0.20 * d
        factors.append(("ERA vs xFIP", round(d, 2), ""))
    if fip is not None and xfip is not None:
        d = xfip - fip; luck += 0.10 * d
        factors.append(("HR suppression (xFIP-FIP)", round(d, 2), "homer-lucky" if d > 0 else "homer-unlucky"))
    if gl_factors.get("babip") is not None:
        d = (LG_BABIP - gl_factors["babip"]) * 12
        luck += 0.18 * d
        factors.append(("BABIP", gl_factors["babip"], "low = lucky on contact" if gl_factors["babip"] < LG_BABIP else "high = unlucky"))
    if gl_factors.get("lob") is not None:
        d = (gl_factors["lob"] - LG_LOB) * 6
        luck += 0.12 * d
        factors.append(("LOB% (strand)", gl_factors["lob"], "high = lucky sequencing" if gl_factors["lob"] > LG_LOB else "low = unlucky"))
    if oor is not None:
        d = (LG_OOR - oor) * 0.05
        luck += 0.05 * d
        factors.append(("Schedule faced (OOR)", round(oor, 1), "soft slate inflated results" if oor < LG_OOR else "tough slate"))

    # skill trend modifies conviction (real change, not luck)
    if gl_factors.get("k_trend") is not None:
        factors.append(("K% trend (L3 vs szn)", gl_factors["k_trend"], ""))
    if gl_factors.get("bb_trend") is not None:
        factors.append(("BB% trend", gl_factors["bb_trend"], ""))
    if gl_factors.get("fatigue") is not None:
        factors.append(("Recent pitch count", gl_factors["fatigue"], "heavy" if gl_factors["fatigue"] >= 100 else ""))

    luck = round(luck, 2)
    verdict, tone = _verdict_from_luck(luck)
    skill_era = round((fip * 0.55 + xfip * 0.45) if (fip is not None and xfip is not None)
                      else (fip if fip is not None else era or 4.10), 2)
    return {"verdict": verdict, "tone": tone, "luck": luck, "luck_base": luck, "matchup_adj": 0.0,
            "skill_era": skill_era, "era": era, "fip": fip, "xfip": xfip, "factors": factors}


def prop_projections(p: dict, reg: dict, ctx: dict | None = None) -> dict:
    """Regression-adjusted projections + over/under leans for K / BB / Outs / ER."""
    avg_ip = _num(p.get("avg_IP")) or 5.3
    k_pct = _num(p.get("K_pct"))
    bb_pct = _num(p.get("BB_pct"))
    if k_pct is not None and k_pct <= 1.5:
        k_pct *= 100
    if bb_pct is not None and bb_pct <= 1.5:
        bb_pct *= 100
    # Matchup platoon nudge on K%/BB% when today's lineup is known
    if ctx and ctx.get("platoon") and ctx["platoon"].get("n", 0) >= 7:
        ph = str(p.get("pitcher_hand", "R")).upper()[:1]
        plat = ctx["platoon"]
        if ph == "R":
            stress = (plat.get("lhh_pct") or LG_LHH_SHARE) - LG_LHH_SHARE
        else:
            stress = (plat.get("rhh_pct") or LG_LHH_SHARE) - LG_LHH_SHARE
        k_pct = (k_pct or 21) - stress * 8
        bb_pct = (bb_pct or 8) + stress * 3
    bf = avg_ip * 4.25  # ~batters faced per start
    outs = round(avg_ip * 3, 1)
    k_proj = round(bf * (k_pct or 21) / 100, 1)
    bb_proj = round(bf * (bb_pct or 8) / 100, 1)
    # ER: regress ERA toward skill_era; park/weather nudge run environment
    blended = (reg["skill_era"] * 0.6 + (reg["era"] or reg["skill_era"]) * 0.4)
    park = ctx.get("park") if ctx else None
    if park is not None:
        blended *= 0.85 + park * 0.15
    er_proj = round(blended / 9 * avg_ip, 1)

    def lean(verdict, stat):
        # regression -> fewer Ks/outs, more ER, slightly more BB; progression opposite
        if verdict == "REGRESSION":
            return {"K": "under", "Outs": "under", "ER": "over", "BB": "over"}[stat]
        if verdict == "PROGRESSION":
            return {"K": "over", "Outs": "over", "ER": "under", "BB": "under"}[stat]
        return "—"

    v = reg["verdict"]
    return {
        "K": {"proj": k_proj, "lean": lean(v, "K")},
        "BB": {"proj": bb_proj, "lean": lean(v, "BB")},
        "Outs": {"proj": outs, "lean": lean(v, "Outs")},
        "ER": {"proj": er_proj, "lean": lean(v, "ER")},
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

    def reg_for(name, *, team=None, opp=None, hand=None, is_home=None, matchup_row=None):
        p = _resolve_pitcher(str(name).strip(), team, hand, index)
        if not p:
            return None
        nm = str(name).strip()
        if nm not in glf_cache:
            glf_cache[nm] = _glfactors_for(nm, gamelog, p)
        reg = regression_read(p, glf_cache[nm])
        if team and opp and hand is not None and matchup_row is not None:
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
            )
            reg = apply_matchup_to_regression(reg, ctx)
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
