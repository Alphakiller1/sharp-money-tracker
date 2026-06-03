"""
Pitcher + general-market analytics for the sharp dashboard.

Regression/progression is NOT just ERA-vs-FIP. We extrapolate the full luck stack
from the mlbma pipeline:
  - ERA vs FIP and vs xFIP        (DIPS skill gap)
  - BABIP vs ~.295                (balls-in-play luck, from the game log)
  - LOB% / strand vs ~72%         (sequencing luck, from the game log)
  - FIP vs xFIP                   (HR-suppression luck)
  - strength of schedule faced    (soft slate inflates results -> mean-reverts)
  - recent K%/BB% trend           (real skill change, not luck)
  - workload / pitch-count fatigue (stuff erosion risk)

Output drives a Pitcher board (regression + K/BB/Outs/ER prop leans) and a
General-Markets board (ML/Total/F5 lean from starter reg/prog + bullpen fatigue +
splits + opponent offense).
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from _compat import load

# League baselines
LG_BABIP, LG_LOB, LG_HR9, LG_OOR, LG_ERA = 0.295, 0.72, 1.15, 44.0, 4.10
RECENT_N = 3


def _num(v):
    try:
        f = float(str(v).replace("%", ""))
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


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
    if luck >= 0.6:
        verdict, tone = "REGRESSION", "fade"
    elif luck <= -0.6:
        verdict, tone = "PROGRESSION", "back"
    else:
        verdict, tone = "STABLE", "neutral"
    skill_era = round((fip * 0.55 + xfip * 0.45) if (fip is not None and xfip is not None)
                      else (fip if fip is not None else era or 4.10), 2)
    return {"verdict": verdict, "tone": tone, "luck": luck, "skill_era": skill_era,
            "era": era, "fip": fip, "xfip": xfip, "factors": factors}


def prop_projections(p: dict, reg: dict) -> dict:
    """Regression-adjusted projections + over/under leans for K / BB / Outs / ER."""
    avg_ip = _num(p.get("avg_IP")) or 5.3
    k_pct = _num(p.get("K_pct"))
    bb_pct = _num(p.get("BB_pct"))
    if k_pct is not None and k_pct <= 1.5:
        k_pct *= 100
    if bb_pct is not None and bb_pct <= 1.5:
        bb_pct *= 100
    bf = avg_ip * 4.25  # ~batters faced per start
    outs = round(avg_ip * 3, 1)
    k_proj = round(bf * (k_pct or 21) / 100, 1)
    bb_proj = round(bf * (bb_pct or 8) / 100, 1)
    # ER: regress ERA toward skill_era; per-start ER = blended/9 * IP
    blended = (reg["skill_era"] * 0.6 + (reg["era"] or reg["skill_era"]) * 0.4)
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
    profiles = load("sp_profiles.csv")
    gamelog = load("sp_gamelog.csv")
    matchups = load("today_matchups.csv")
    if profiles is None:
        return []
    profiles = profiles.copy()
    profiles["__name"] = profiles["pitcher_name"].astype(str).str.strip()

    today_sp = {}  # name -> (team, opp, hand)
    if matchups is not None:
        for _, g in matchups.iterrows():
            a, h = str(g.get("Away", "")).upper(), str(g.get("Home", "")).upper()
            for sp_col, hand_col, team, opp in ((("Away_SP",), ("Away_Hand",), a, h), (("Home_SP",), ("Home_Hand",), h, a)):
                nm = str(g.get(sp_col[0], "")).strip()
                if nm and nm.lower() != "tbd":
                    today_sp[nm] = (team, opp, str(g.get(hand_col[0], "R")).upper()[:1])

    rows = []
    for _, p in profiles.iterrows():
        name = p["__name"]
        glf = _gamelog_factors(gamelog[gamelog["pitcher_name"].astype(str).str.strip() == name]) if gamelog is not None else {}
        reg = regression_read(p.to_dict(), glf)
        props = prop_projections(p.to_dict(), reg)
        is_today = name in today_sp
        team, opp, hand = today_sp.get(name, (str(p.get("pitcher_team", "")).upper(), "", str(p.get("pitcher_hand", "R")).upper()[:1]))
        rows.append({
            "name": name, "team": team, "opp": opp, "hand": hand, "today": is_today,
            "starts": int(_num(p.get("starts")) or 0),
            **reg, "props": props,
        })
    # today's starters first, then by |luck| desc
    rows.sort(key=lambda r: (not r["today"], -abs(r["luck"])))
    return rows


def market_board() -> list[dict]:
    matchups = load("today_matchups.csv")
    profiles = load("sp_profiles.csv")
    teams = load("team_profiles.csv")
    if matchups is None:
        return []
    prof_by_name = {str(r["pitcher_name"]).strip(): r.to_dict() for _, r in profiles.iterrows()} if profiles is not None else {}
    bp_by_team = {}
    if teams is not None:
        for _, t in teams.iterrows():
            bp_by_team[str(t.get("team", "")).upper()] = t.to_dict()

    glf_cache = {}
    gamelog = load("sp_gamelog.csv")

    def reg_for(name):
        p = prof_by_name.get(str(name).strip())
        if not p:
            return None
        if name not in glf_cache and gamelog is not None:
            glf_cache[name] = _gamelog_factors(gamelog[gamelog["pitcher_name"].astype(str).str.strip() == str(name).strip()])
        return regression_read(p, glf_cache.get(name, {}))

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
        a_reg = reg_for(g.get("Away_SP"))
        h_reg = reg_for(g.get("Home_SP"))
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
            "away_skill": a_reg["skill_era"] if a_reg else None, "home_skill": h_reg["skill_era"] if h_reg else None,
            "away_verdict": a_reg["verdict"] if a_reg else None, "home_verdict": h_reg["verdict"] if h_reg else None,
            "away_bp": a_bp["score"] if a_bp else None, "home_bp": h_bp["score"] if h_bp else None,
            "ml_lean": ml_lean, "total_lean": total_lean, "f5_lean": f5_lean,
            "drivers": drivers[:4],
        })
    return rows
