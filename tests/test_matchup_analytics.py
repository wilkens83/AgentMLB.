"""Unit tests for the matchup analytics helpers, agent prompt, and multi-table
SQL safety. All pure/offline — no database, LLM, or network.
"""
import db
import agent
import matchup


# --------------------------------------------------------------------------- #
# BVP aggregation logic (mirrors mv_bvp_matchups)
# --------------------------------------------------------------------------- #
def test_bvp_line_slash_stats():
    events = ["single", "double", "home_run", "triple", "strikeout",
              "field_out", "walk", "hit_by_pitch", "sac_fly"]
    line = matchup.bvp_line(events)
    assert line["h"] == 4          # single, double, triple, home_run
    assert line["hr"] == 1
    assert line["bb"] == 1
    assert line["hbp"] == 1
    assert line["sf"] == 1
    assert line["ab"] == 6         # excludes walk, hbp, sac_fly
    assert line["tb"] == 10        # 1 + 2 + 3 + 4
    assert line["avg"] == round(4 / 6, 3)
    assert line["obp"] == round(6 / 9, 3)      # (4+1+1)/(6+1+1+1)
    assert line["slg"] == round(10 / 6, 3)
    assert line["ops"] == round(round(6 / 9, 3) + round(10 / 6, 3), 3)


def test_bvp_line_empty_has_no_divide_by_zero():
    line = matchup.bvp_line([])
    assert line["ab"] == 0
    assert line["avg"] is None
    assert line["obp"] is None
    assert line["ops"] is None


def test_bvp_line_ignores_none_events():
    line = matchup.bvp_line([None, "", "strikeout", "single"])
    assert line["ab"] == 2
    assert line["h"] == 1


# --------------------------------------------------------------------------- #
# Sample-size fallback + highlight rules
# --------------------------------------------------------------------------- #
def test_needs_handedness_fallback():
    assert matchup.needs_handedness_fallback(0) is True
    assert matchup.needs_handedness_fallback(4) is True
    assert matchup.needs_handedness_fallback(5) is False
    assert matchup.needs_handedness_fallback(20) is False


def test_should_highlight_rules():
    assert matchup.should_highlight(0.950, 3) is True     # OPS >= .900
    assert matchup.should_highlight(0.500, 12) is True     # AB >= 10
    assert matchup.should_highlight(0.500, 4) is False
    assert matchup.should_highlight(None, 4) is False


# --------------------------------------------------------------------------- #
# IP / rate math
# --------------------------------------------------------------------------- #
def test_outs_and_innings_pitched():
    events = ["strikeout", "field_out", "grounded_into_double_play"]  # 1+1+2 = 4 outs
    assert matchup.outs_recorded(events) == 4
    assert matchup.innings_pitched(events) == round(4 / 3, 1)


def test_k9_bb9_and_rate_advantage():
    assert matchup.k_per_9(9, 9.0) == 9.0
    assert matchup.bb_per_9(3, 9.0) == 3.0
    assert matchup.per_9(5, 0) is None                    # divide-by-zero guard
    # Starter A K/9 9.5 vs B 7.0 -> +2.5 advantage to A.
    assert matchup.rate_advantage(9.5, 7.0) == 2.5
    assert matchup.rate_advantage(2.0, 3.5) == -1.5
    assert matchup.rate_advantage(None, 3.0) is None


# --------------------------------------------------------------------------- #
# Multi-table join SQL passes the read-only guard + row cap
# --------------------------------------------------------------------------- #
def test_multi_table_join_is_readonly_and_capped():
    sql = (
        "SELECT b.batter, b.ops, t.season_ra9, t.blowup_pct "
        "FROM mv_bvp_matchups b "
        "JOIN mv_pitcher_trends_and_blowup t ON b.pitcher = t.pitcher "
        "WHERE b.pitcher_name ILIKE '%Cole, Gerrit%' AND b.ab >= 5 "
        "ORDER BY b.ops DESC"
    )
    assert db.assert_readonly_sql(sql)
    assert db.apply_row_limit(sql).rstrip().endswith("LIMIT 1000")


def test_bvp_join_with_handedness_fallback_is_readonly():
    # Career split vs handedness fallback joins statcast_pitches to the BVP view.
    sql = (
        "WITH h2h AS (SELECT batter FROM mv_bvp_matchups WHERE pitcher = 543037 AND ab < 5) "
        "SELECT s.batter, s.p_throws, COUNT(*) "
        "FROM statcast_pitches s JOIN h2h ON s.batter = h2h.batter "
        "WHERE s.p_throws = 'R' GROUP BY s.batter, s.p_throws"
    )
    assert db.assert_readonly_sql(sql)


# --------------------------------------------------------------------------- #
# Agent prompt exposes the new analytics + honest data-gap handling
# --------------------------------------------------------------------------- #
def test_system_prompt_documents_matchup_views():
    p = agent.SYSTEM_PROMPT
    for view in ("mv_bvp_matchups", "mv_pitcher_trends_and_blowup",
                 "mv_bullpen_l10", "v_team_platoon_ops"):
        assert view in p


def test_system_prompt_uses_ra9_not_era_and_flags_missing_data():
    p = agent.SYSTEM_PROMPT
    assert "RA/9" in p or "ra9" in p
    assert "NOT AVAILABLE" in p          # weather + salary honesty
    assert "career split" in p.lower()   # BVP sample-size fallback rule
