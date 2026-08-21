"""
src/matchup.py - Pure matchup-analytics helpers for the PitcherStat Grille.

These functions mirror the SQL logic in scripts/003_add_matchup_analytics.sql
(BVP slash line, IP-from-outs, per-9 rates) so the same math can be unit-tested
offline and reused by the Streamlit matchup tab. No database or network needed.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

# Event classification (must match the SQL definitions).
HIT_EVENTS = {"single", "double", "triple", "home_run"}
NON_AB_EVENTS = {
    "walk", "hit_by_pitch", "sac_fly", "sac_bunt", "catcher_interf",
    "sac_fly_double_play", "sac_bunt_double_play",
}
SF_EVENTS = {"sac_fly", "sac_fly_double_play"}
TOTAL_BASES = {"single": 1, "double": 2, "triple": 3, "home_run": 4}

# Outs recorded per terminal event (mirrors the SQL CASE for IP derivation).
OUTS_BY_EVENT = {
    "strikeout": 1, "field_out": 1, "force_out": 1, "sac_fly": 1, "sac_bunt": 1,
    "fielders_choice_out": 1, "fielders_choice": 1, "other_out": 1,
    "strikeout_double_play": 2, "grounded_into_double_play": 2, "double_play": 2,
    "sac_fly_double_play": 2, "sac_bunt_double_play": 2, "triple_play": 3,
}

# Head-to-head sample below which a career handedness split should be appended.
BVP_MIN_AB = 5
# Thresholds for highlighting a batter in the BVP grid.
HIGHLIGHT_OPS = 0.900
HIGHLIGHT_AB = 10


def _round(value: Optional[float], ndigits: int = 3) -> Optional[float]:
    return None if value is None else round(value, ndigits)


def outs_recorded(events: Iterable[Optional[str]]) -> int:
    """Total outs recorded across a sequence of terminal `events` values."""
    return sum(OUTS_BY_EVENT.get(e or "", 0) for e in events)


def innings_pitched(events: Iterable[Optional[str]]) -> float:
    """Innings pitched derived from outs (outs / 3), rounded to tenths."""
    return round(outs_recorded(events) / 3.0, 1)


def bvp_line(events: Iterable[Optional[str]]) -> Dict[str, float]:
    """Compute a batter-vs-pitcher slash line from terminal `events` values.

    Returns a dict with AB, H, HR, BB, HBP, SF, TB, AVG, OBP, SLG, OPS.
    Rate stats are None when the denominator is zero (mirrors SQL NULLIF).
    """
    ev = [e for e in events if e]
    h = sum(1 for e in ev if e in HIT_EVENTS)
    hr = sum(1 for e in ev if e == "home_run")
    bb = sum(1 for e in ev if e == "walk")
    hbp = sum(1 for e in ev if e == "hit_by_pitch")
    sf = sum(1 for e in ev if e in SF_EVENTS)
    ab = sum(1 for e in ev if e not in NON_AB_EVENTS)
    tb = sum(TOTAL_BASES.get(e, 0) for e in ev)

    avg = _round(h / ab, 3) if ab else None
    obp_denom = ab + bb + hbp + sf
    obp = _round((h + bb + hbp) / obp_denom, 3) if obp_denom else None
    slg = _round(tb / ab, 3) if ab else None
    ops = _round((obp or 0) + (slg or 0), 3) if (obp is not None or slg is not None) else None

    return {
        "ab": ab, "h": h, "hr": hr, "bb": bb, "hbp": hbp, "sf": sf, "tb": tb,
        "avg": avg, "obp": obp, "slg": slg, "ops": ops,
    }


def needs_handedness_fallback(ab: int, min_ab: int = BVP_MIN_AB) -> bool:
    """True when the head-to-head sample is too small (AB < min_ab) and the
    batter's career split vs the pitcher's handedness should be appended."""
    return ab < min_ab


def should_highlight(ops: Optional[float], ab: int) -> bool:
    """BVP grid highlight rule: OPS >= .900 OR sample size >= 10 AB."""
    return (ops is not None and ops >= HIGHLIGHT_OPS) or ab >= HIGHLIGHT_AB


def per_9(count: float, innings: float) -> Optional[float]:
    """Generic per-9-innings rate (e.g. K/9, BB/9). None if innings == 0."""
    if not innings:
        return None
    return round(count * 9.0 / innings, 2)


def k_per_9(strikeouts: float, innings: float) -> Optional[float]:
    """Strikeouts per 9 innings."""
    return per_9(strikeouts, innings)


def bb_per_9(walks: float, innings: float) -> Optional[float]:
    """Walks per 9 innings."""
    return per_9(walks, innings)


def rate_advantage(rate_a: Optional[float], rate_b: Optional[float]) -> Optional[float]:
    """Comparative advantage (Starter A - Starter B) for a per-9 rate.

    Positive means Starter A has the higher value. For K/9 higher is better
    (advantage to A); for BB/9 lower is better (a positive result favors B).
    Returns None if either input is None.
    """
    if rate_a is None or rate_b is None:
        return None
    return round(rate_a - rate_b, 2)
