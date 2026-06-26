"""
Pitcher projection layers — data caches + channel-separated adjustments.

K/BB channel: L14 rates, pitch mix, Savant discipline, gamelog trends, signals (K/BB only).
ER channel: lineup projOSI, OSI tiers, PitchScore anchors, park/weather, rolling opp OSI, L10 hand.
Outs/IP channel: bullpen quick-hook, rest/workload (anti double-count: no offense on K/BB).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

import pandas as pd

import config
from _compat import load, load_pitch_mix

LG_LHH_SHARE = 0.42
L14_BLEND = 0.55
L14_MIN_STARTS = 2
OSI_HIGH, OSI_LOW = 58.0, 42.0
SAVANT_KBB_SCALE = 2.5
SIGNAL_CONV_BOOST = 0.35
ROLLING_OSI_N = 5
L10_HAND_ER_SCALE = 0.04


def _norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def _num(v):
    try:
        f = float(str(v).replace("%", ""))
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _pct(v) -> float | None:
    x = _num(v)
    if x is None:
        return None
    return x * 100 if x <= 1.5 else x


def _osi_tier(osi: float | None) -> str:
    if osi is None:
        return "Mid"
    if osi >= OSI_HIGH:
        return "High"
    if osi <= OSI_LOW:
        return "Low"
    return "Mid"


def _effective_platoon_side(bats: str, pitcher_hand: str) -> str:
    b = str(bats or "R").strip().upper()[:1]
    ph = str(pitcher_hand or "R").strip().upper()[:1]
    if b == "S":
        return "L" if ph == "R" else "R"
    return b if b in ("L", "R") else "R"


@dataclass
class ModelCaches:
  l14_by_name: dict[str, dict] = field(default_factory=dict)
  osi_splits: dict[tuple, dict] = field(default_factory=dict)
  batter_by_name: dict[str, list[dict]] = field(default_factory=dict)
  batter_mix_by_id: dict[int, pd.DataFrame] = field(default_factory=dict)
  batter_mix_by_name: dict[str, pd.DataFrame] = field(default_factory=dict)
  team_savant: dict[str, dict] = field(default_factory=dict)
  team_profiles: dict[str, dict] = field(default_factory=dict)
  team_l10_hand: dict[tuple[str, str], dict] = field(default_factory=dict)
  signals: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
  rolling_opp_osi: dict[str, float] = field(default_factory=dict)
  pitch_mix_batter: pd.DataFrame | None = None


def build_caches(gamelog: pd.DataFrame | None = None) -> ModelCaches:
    c = ModelCaches()

    l14 = load("sp_l14.csv")
    if l14 is not None and not l14.empty:
        name_col = "Name" if "Name" in l14.columns else "pitcher_name"
        for _, r in l14.iterrows():
            nm = _norm_name(r.get(name_col))
            if nm:
                c.l14_by_name[nm] = r.to_dict()

    splits = load("sp_metric_splits.csv")
    if splits is not None and not splits.empty:
        sub = splits[splits["split_dimension"] == "osi_tier"]
        for _, r in sub.iterrows():
            pid = _num(r.get("pitcher_id"))
            tier = str(r.get("split_value", "")).strip()
            key = (int(pid), tier) if pid else (_norm_name(r.get("pitcher_name")), tier)
            c.osi_splits[key] = r.to_dict()

    bp = load("batter_profiles.csv")
    if bp is not None and not bp.empty:
        for _, r in bp.iterrows():
            c.batter_by_name.setdefault(_norm_name(r.get("player_name")), []).append(r.to_dict())

    bm = load_pitch_mix("batter")
    if bm is not None and not bm.empty:
        c.pitch_mix_batter = bm
        for pid, sub in bm.groupby("player_id"):
            pid_i = int(_num(pid) or 0)
            if pid_i:
                c.batter_mix_by_id[pid_i] = sub
        if "full_name" in bm.columns:
            for nm, sub in bm.groupby(bm["full_name"].astype(str).map(_norm_name)):
                if nm:
                    c.batter_mix_by_name[nm] = sub

    sav = load("savant_team_leaderboard.csv")
    if sav is not None and not sav.empty:
        col = "Tm" if "Tm" in sav.columns else "team"
        for _, r in sav.iterrows():
            tm = str(r.get(col, "")).upper().strip()
            if tm:
                c.team_savant[tm] = r.to_dict()

    teams = load("team_profiles.csv")
    if teams is not None and not teams.empty:
        for _, r in teams.iterrows():
            c.team_profiles[str(r.get("team", "")).upper()] = r.to_dict()

    l10 = load("team_l10_sp_hand.csv")
    if l10 is not None and not l10.empty:
        for _, r in l10.iterrows():
            tm = str(r.get("team", "")).upper()
            hand = str(r.get("opp_starter_hand", "R")).upper()[:1]
            if tm and r.get("games"):
                c.team_l10_hand[(tm, hand)] = r.to_dict()

    sig = load("signals_today.csv")
    if sig is not None and not sig.empty:
        for _, r in sig.iterrows():
            tm = str(r.get("away") if str(r.get("side", "")).lower() == "away" else r.get("home", "")).upper()
            side = str(r.get("side", "")).lower()
            fired = str(r.get("fired", "")).lower() in ("true", "1", "yes")
            if tm and side:
                c.signals.setdefault((tm, side), []).append({**r.to_dict(), "_fired": fired})

    if gamelog is not None and not gamelog.empty and "opponent_OSI" in gamelog.columns:
        gl = gamelog.copy()
        if "date" in gl.columns:
            gl = gl.sort_values("date")
        for nm, sub in gl.groupby(gl["pitcher_name"].astype(str).map(_norm_name)):
            recent = sub.tail(ROLLING_OSI_N)
            vals = pd.to_numeric(recent["opponent_OSI"], errors="coerce").dropna()
            if len(vals):
                c.rolling_opp_osi[nm] = float(vals.mean())

    return c


def l14_blend(profile: dict, caches: ModelCaches, name: str) -> tuple[dict, list[dict]]:
    """Blend season K%/BB%/FIP with L14 when sample is fresh."""
    factors: list[dict] = []
    out = {"k_pct": _pct(profile.get("K_pct")), "bb_pct": _pct(profile.get("BB_pct")),
           "fip": _num(profile.get("FIP")), "blend_weight": 0.0}
    stale = str(profile.get("stale", "")).lower() in ("true", "1") or bool(profile.get("stale"))
    l14_starts = int(_num(profile.get("l14_starts")) or 0)
    l14 = caches.l14_by_name.get(_norm_name(name))
    if stale or l14_starts < L14_MIN_STARTS or not l14:
        if stale or l14_starts < L14_MIN_STARTS:
            warn = str(profile.get("staleness_warning") or f"L14 n={l14_starts} — season rates")
            factors.append({
                "key": "staleness", "label": "Rate staleness",
                "value": l14_starts, "note": warn,
            })
        return out, factors

    lk = _pct(l14.get("K%"))
    lb = _pct(l14.get("BB%"))
    lf = _num(l14.get("FIP"))
    w = L14_BLEND
    out["blend_weight"] = w
    if lk is not None and out["k_pct"] is not None:
        out["k_pct"] = round(out["k_pct"] * (1 - w) + lk * w, 2)
    if lb is not None and out["bb_pct"] is not None:
        out["bb_pct"] = round(out["bb_pct"] * (1 - w) + lb * w, 2)
    if lf is not None and out["fip"] is not None:
        out["fip"] = round(out["fip"] * (1 - w) + lf * w, 2)
    factors.append({
        "key": "l14_blend", "label": "L14 rate blend",
        "value": round(w, 2),
        "note": f"{l14_starts} L14 starts — {w*100:.0f}% recent K/BB/FIP",
    })
    return out, factors


def osi_tier_skill(profile: dict, caches: ModelCaches, opp_osi: float | None, name: str) -> tuple[float | None, list[dict]]:
    factors: list[dict] = []
    tier = _osi_tier(opp_osi)
    pid = _num(profile.get("pitcher_id"))
    row = None
    if pid:
        row = caches.osi_splits.get((int(pid), tier))
    if row is None:
        row = caches.osi_splits.get((_norm_name(name), tier))
    if not row:
        return None, factors
    tier_fip = _num(row.get("FIP"))
    if tier_fip is None:
        return None, factors
    factors.append({
        "key": "osi_tier_fip", "label": f"SP vs {tier} OSI tier FIP",
        "value": round(tier_fip, 2),
        "note": f"opp OSI {opp_osi:.1f} → {tier} tier ({int(_num(row.get('starts')) or 0)} starts)" if opp_osi is not None
        else f"{tier} OSI tier ({int(_num(row.get('starts')) or 0)} starts)",
    })
    return tier_fip, factors


def lineup_weighted_proj_osi(
    lineups: pd.DataFrame | None, opp_team: str, pitcher_hand: str, caches: ModelCaches,
) -> tuple[float | None, list[dict]]:
    factors: list[dict] = []
    if lineups is None or lineups.empty or not opp_team:
        return None, factors
    sub = lineups[lineups["Team"].astype(str).str.upper().str.strip() == str(opp_team).upper()]
    if sub.empty:
        return None, factors
    split = "vs_RHP" if str(pitcher_hand).upper().startswith("R") else "vs_LHP"
    vals, weights = [], []
    for _, r in sub.iterrows():
        pname = str(r.get("Player", "")).strip()
        nk = _norm_name(pname)
        rows = caches.batter_by_name.get(nk, [])
        hit = next((x for x in rows if str(x.get("split_type")) == split), None)
        if hit is None:
            hit = next((x for x in rows if str(x.get("split_type")) == "overall"), None)
        if not hit:
            continue
        pos = str(r.get("Position", "")).upper()
        w = 1.2 if pos in ("DH", "1B", "3B", "LF", "RF", "C") else 1.0
        posi = _num(hit.get("projOSI")) or _num(hit.get("OSI"))
        if posi is not None:
            vals.append(posi * w)
            weights.append(w)
    if not vals:
        return None, factors
    avg = sum(vals) / sum(weights)
    factors.append({
        "key": "lineup_proj_osi", "label": "Lineup-weighted projOSI",
        "value": round(avg, 1),
        "note": f"{len(vals)} batters · {split} splits",
    })
    return avg, factors


def lineup_pitch_mix_kbb(
    lineups: pd.DataFrame | None, opp_team: str, pitcher_hand: str, caches: ModelCaches,
    pitcher_mix_rows: pd.DataFrame | None,
) -> tuple[float, float, list[dict]]:
    """Lineup-level pitch mix: aggregate batter usage vs pitcher arsenal."""
    factors: list[dict] = []
    if lineups is None or pitcher_mix_rows is None or caches.pitch_mix_batter is None:
        return 0.0, 0.0, factors
    sub = lineups[lineups["Team"].astype(str).str.upper() == str(opp_team).upper()]
    if sub.empty:
        return 0.0, 0.0, factors
    pit_by_type = {str(r["pitch_type"]): r for _, r in pitcher_mix_rows.iterrows()}
    scores = []
    for _, r in sub.iterrows():
        nk = _norm_name(r.get("Player"))
        brow = caches.batter_mix_by_name.get(nk)
        if brow is None:
            continue
        for _, br in brow.iterrows():
            pt = str(br.get("pitch_type", ""))
            pr = pit_by_type.get(pt)
            if pr is None:
                continue
            usage = (_num(br.get("pitch_pct")) or 0) / 100
            pit_whiff = (_num(pr.get("whiff_rate")) or 0) / 100
            bat_whiff = (_num(br.get("whiff_rate")) or 0) / 100
            scores.append(usage * (pit_whiff - bat_whiff))
    if not scores:
        return 0.0, 0.0, factors
    total = sum(scores) / len(scores)
    k_delta = round(total * 8, 2)
    bb_delta = round(-total * 2.5, 2)
    factors.append({
        "key": "lineup_pitch_mix", "label": "Lineup pitch-mix K/BB",
        "value": round(total, 3),
        "note": "batter-level usage vs SP arsenal (K/BB channel only)",
    })
    return k_delta, bb_delta, factors


def savant_kbb_modifier(opp_team: str, caches: ModelCaches) -> tuple[float, float, list[dict]]:
    factors: list[dict] = []
    row = caches.team_savant.get(str(opp_team).upper())
    if not row:
        return 0.0, 0.0, factors
    swstr = _num(row.get("SwStr%"))
    chase = _num(row.get("Chase%"))
    if swstr is None:
        return 0.0, 0.0, factors
    lg_swstr, lg_chase = 11.5, 28.0
    k_delta = round((lg_swstr - swstr) * SAVANT_KBB_SCALE / 10, 2)
    bb_delta = round((chase - lg_chase) * SAVANT_KBB_SCALE / 20 if chase else 0, 2)
    factors.append({
        "key": "savant_discipline", "label": "Opp Savant discipline",
        "value": round(swstr, 1),
        "note": f"SwStr {swstr:.1f}% · Chase {chase or 0:.1f}% vs league",
    })
    return k_delta, bb_delta, factors


def bullpen_hook_factor(team: str, caches: ModelCaches) -> tuple[float, list[dict]]:
    factors: list[dict] = []
    t = caches.team_profiles.get(str(team).upper())
    if not t:
        return 1.0, factors
    hilev = _num(t.get("bullpen_high_lev_era"))
    ir = _num(t.get("bullpen_ir_scored_pct"))
    outs_factor = 1.0
    if hilev is not None and hilev > 4.2:
        outs_factor -= min(0.12, (hilev - 4.2) * 0.03)
    if ir is not None and ir > 35:
        outs_factor -= min(0.08, (ir - 35) * 0.004)
    outs_factor = max(0.82, outs_factor)
    if outs_factor < 0.99:
        factors.append({
            "key": "bullpen_hook", "label": "Bullpen quick-hook risk",
            "value": round(outs_factor, 3),
            "note": f"HI-lev ERA {hilev or '—'} · IR scored {ir or '—'}%",
        })
    return outs_factor, factors


def rest_workload(gamelog_slice: pd.DataFrame, profile: dict) -> tuple[float, float, float, list[dict]]:
    """days_rest, outs_factor, k_pct_delta, factors."""
    factors: list[dict] = []
    outs_factor, k_delta = 1.0, 0.0
    if gamelog_slice.empty:
        return 0.0, outs_factor, k_delta, factors
    gl = gamelog_slice.copy()
    if "date" in gl.columns:
        gl = gl.sort_values("date")
    last = gl.iloc[-1]
    days_rest = 4.0
    try:
        last_d = datetime.strptime(str(last["date"])[:10], "%Y-%m-%d").date()
        days_rest = max(0, (date.today() - last_d).days)
    except (ValueError, TypeError):
        pass
    pitches = _num(last.get("pitches")) or _num(profile.get("avg_pitches"))
    if pitches and pitches >= 100:
        outs_factor -= min(0.10, (pitches - 95) * 0.008)
        k_delta -= min(2.0, (pitches - 95) * 0.015)
    if days_rest <= 3:
        outs_factor -= 0.04
        k_delta -= 0.8
    elif days_rest >= 6:
        outs_factor += 0.03
    outs_factor = max(0.80, min(1.08, outs_factor))
    factors.append({
        "key": "rest_workload", "label": "Rest / workload",
        "value": days_rest,
        "note": f"{days_rest:.0f}d rest · last start {int(pitches or 0)} pitches",
    })
    return days_rest, outs_factor, k_delta, factors


def signal_conviction(
    team: str, opp: str, is_home: bool, caches: ModelCaches,
) -> tuple[dict[str, float], list[dict]]:
    """K/BB lean conviction boosts from signals_today (no duplicate offense on ER)."""
    factors: list[dict] = []
    boosts = {"K": 0.0, "BB": 0.0}
    opp_side = "home" if not is_home else "away"
    opp_sigs = caches.signals.get((str(opp).upper(), opp_side), [])
    for s in opp_sigs:
        if not s.get("_fired"):
            continue
        nm = str(s.get("signal_name", ""))
        mag = abs(_num(s.get("magnitude")) or 0)
        if "K%" in nm and "OBR" in nm:
            boosts["K"] += SIGNAL_CONV_BOOST * min(1.0, mag / 3)
            factors.append({
                "key": "signal_k", "label": "Signal: K% vs OBR",
                "value": round(boosts["K"], 2), "note": str(s.get("verdict_text", ""))[:80],
            })
        elif "BB%" in nm and "ABQ" in nm:
            boosts["BB"] += SIGNAL_CONV_BOOST * min(1.0, mag / 3)
            factors.append({
                "key": "signal_bb", "label": "Signal: BB% vs ABQ",
                "value": round(boosts["BB"], 2), "note": str(s.get("verdict_text", ""))[:80],
            })
    return boosts, factors


def pitchscore_anchor(matchup_row: dict | None, team: str, is_home: bool) -> tuple[float | None, list[dict]]:
    factors: list[dict] = []
    if not matchup_row:
        return None, factors
    col = "Home_PitchScore" if is_home else "Away_PitchScore"
    ps = _num(matchup_row.get(col))
    if ps is None:
        return None, factors
    fip_est = config.LEAGUE_FIP - (ps - 50) * 0.012
    factors.append({
        "key": "pitchscore_anchor", "label": "PitchScore anchor FIP",
        "value": round(ps, 1),
        "note": f"PitchScore {ps:.1f} → est FIP {fip_est:.2f}",
    })
    return fip_est, factors


def rolling_opp_osi_factor(name: str, opp_osi: float | None, caches: ModelCaches) -> tuple[float, list[dict]]:
    factors: list[dict] = []
    roll = caches.rolling_opp_osi.get(_norm_name(name))
    if roll is None or opp_osi is None:
        return 0.0, factors
    delta = roll - opp_osi
    er_mult = 1.0 + delta * 0.003
    factors.append({
        "key": "rolling_opp_osi", "label": "Rolling opp OSI context",
        "value": round(roll, 1),
        "note": f"L{ROLLING_OSI_N} avg {roll:.1f} vs today {opp_osi:.1f}",
    })
    return er_mult - 1.0, factors


def team_l10_hand_check(opp: str, pitcher_hand: str, caches: ModelCaches) -> tuple[float, list[dict]]:
    factors: list[dict] = []
    row = caches.team_l10_hand.get((str(opp).upper(), str(pitcher_hand).upper()[:1]))
    if not row:
        return 0.0, factors
    wrc = _num(row.get("wrc_plus"))
    if wrc is None:
        return 0.0, factors
    push = (wrc - 100) * L10_HAND_ER_SCALE / 100
    factors.append({
        "key": "team_l10_hand", "label": "Opp L10 vs SP hand",
        "value": round(wrc, 0),
        "note": f"{int(_num(row.get('games')) or 0)} games · wRC+ {wrc:.0f}",
    })
    return push, factors


def offense_er_push(
    lineup_osi: float | None, opp_osi: float | None, team_wrc: float | None,
) -> tuple[float, list[dict]]:
    """ER-channel offense (replaces half of old OSI+wRC luck stacking)."""
    factors: list[dict] = []
    push = 0.0
    if lineup_osi is not None:
        push += (lineup_osi - 50) * 0.022 * config.OSI_RUN_SENSITIVITY
    elif opp_osi is not None:
        push += (opp_osi - 50) * 0.018 * config.OSI_RUN_SENSITIVITY
    if team_wrc is not None:
        push += (team_wrc - 100) * 0.006
    if push:
        factors.append({
            "key": "offense_er", "label": "Offense ER pressure",
            "value": round(push, 3),
            "note": "lineup projOSI + residual team wRC (ER channel only)",
        })
    return push, factors


def f5_projection(profile: dict, skill_era: float, er_factor: float) -> tuple[dict, list[dict]]:
    factors: list[dict] = []
    f5_era = _num(profile.get("F5_ERA"))
    if f5_era is None:
        f5_era = skill_era * 0.92
    f5_er = round(f5_era / 9 * 5 * er_factor, 2)
    factors.append({
        "key": "f5_era", "label": "F5 ERA anchor",
        "value": round(f5_era, 2), "note": "first-five run environment",
    })
    return {"proj": f5_er, "lean": "—", "f5_era": round(f5_era, 2)}, factors
