"""
src/app.py - Streamlit dashboard for the MLB Statcast analytics system.

Two tabs:
  * "Chat" - natural-language Text-to-SQL over Supabase (Plotly + CSV export).
  * "Matchup Duel & BVP Analyzer" - starter-vs-starter cards, batter-vs-pitcher
    grid, and bullpen/platoon comparisons built on the migration-003 views.

Note: ERA is not derivable from Statcast (no earned-run flag); the matchup tab
shows RA/9 (Runs Allowed per 9) as the ERA proxy.

Run with:  ``streamlit run src/app.py``
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

# Make ``from agent import ...`` resolve when run as ``streamlit run src/app.py``.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import matchup  # noqa: E402
from agent import (  # noqa: E402
    build_figure,
    dataframe_to_csv_bytes,
    query_mlb_agent,
)

RECOMMENDED_QUERIES = [
    "Compare exit velocity and launch angle for Shohei Ohtani barrels in 2024.",
    "Aaron Judge barrels against 95+ mph fastballs in 2024.",
    "Gerrit Cole slider whiff rate by season.",
    "Top 10 sweeper spin rates in 2024.",
    "League-wide hard-hit percentage by pitch type in 2024.",
]

MATCHUP_PRESETS = [
    "Analyze Today's Dodgers vs. Rockies Pitcher Duel & BVP",
    "Show St. Louis vs. Cincinnati Bullpen Fatigue & Starters",
    "Rank Starters by Lowest Blowup % in 2024",
]

# Outs-from-events CASE, shared by the K/9 & BB/9 card query (mirrors the SQL views).
_OUTS_CASE = """CASE events
    WHEN 'strikeout' THEN 1 WHEN 'field_out' THEN 1 WHEN 'force_out' THEN 1
    WHEN 'sac_fly' THEN 1 WHEN 'sac_bunt' THEN 1 WHEN 'fielders_choice_out' THEN 1
    WHEN 'fielders_choice' THEN 1 WHEN 'other_out' THEN 1
    WHEN 'strikeout_double_play' THEN 2 WHEN 'grounded_into_double_play' THEN 2
    WHEN 'double_play' THEN 2 WHEN 'sac_fly_double_play' THEN 2
    WHEN 'sac_bunt_double_play' THEN 2 WHEN 'triple_play' THEN 3 ELSE 0 END"""


def _esc(text: str) -> str:
    """Escape a single-quoted SQL literal (defense against quote breakage)."""
    return (text or "").replace("'", "''")


def safe_query(sql: str):
    """Run a read-only query, returning (DataFrame, error_message_or_None).

    All DB access flows through db.execute_readonly_query (SELECT-only + row cap).
    Any failure (no DATABASE_URL, network blocked, empty data) is returned as a
    message so the UI can degrade gracefully instead of crashing.
    """
    try:
        from db import execute_readonly_query

        return execute_readonly_query(sql), None
    except Exception as exc:  # noqa: BLE001
        return pd.DataFrame(), str(exc)


# --------------------------------------------------------------------------- #
# Chat tab
# --------------------------------------------------------------------------- #
def render_result(result: dict) -> None:
    """Render a single agent result: explanation, SQL, chart, table, download."""
    if result.get("error"):
        st.error(f"Query failed: {result['error']}")
        if result.get("sql"):
            with st.expander("View Generated SQL"):
                st.code(result["sql"], language="sql")
        return

    explanation = result.get("explanation")
    if explanation:
        st.markdown(explanation)

    sql = result.get("sql", "")
    if sql:
        with st.expander("View Generated SQL"):
            st.code(sql, language="sql")

    df: pd.DataFrame = result.get("data", pd.DataFrame())
    if df is None or df.empty:
        st.info("No rows returned for this query.")
        return

    st.caption(f"Retrieved {len(df):,} rows.")
    fig = build_figure(df, result.get("chart_type", "none"), result.get("chart_config", {}))
    if fig is not None:
        st.plotly_chart(fig, use_container_width=True)

    st.dataframe(df, use_container_width=True, hide_index=True)
    st.download_button(
        label=f"Download CSV ({len(df):,} rows)",
        data=dataframe_to_csv_bytes(df),
        file_name="mlb_statcast_result.csv",
        mime="text/csv",
    )


def render_chat_tab() -> None:
    for msg in st.session_state.get("messages", []):
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                render_result(msg["result"])


# --------------------------------------------------------------------------- #
# Matchup tab
# --------------------------------------------------------------------------- #
def _starter_card_query(name: str, year: int) -> str:
    return f"""
WITH gp AS (
    SELECT events, {_OUTS_CASE} AS outs
    FROM statcast_pitches
    WHERE player_name ILIKE '%{_esc(name)}%' AND game_year = {int(year)}
),
rates AS (
    SELECT round(sum(outs) / 3.0, 1) AS ip,
        count(*) FILTER (WHERE events = 'strikeout') AS k,
        count(*) FILTER (WHERE events = 'walk') AS bb
    FROM gp
),
trend AS (
    SELECT season_ra9, avg_ip_per_start, blowup_pct, ra_trend
    FROM mv_pitcher_trends_and_blowup t
    JOIN statcast_pitches s ON s.pitcher = t.pitcher
    WHERE s.player_name ILIKE '%{_esc(name)}%' AND t.game_year = {int(year)}
    ORDER BY t.game_date DESC LIMIT 1
)
SELECT r.ip, r.k, r.bb,
    round(r.k * 9.0 / NULLIF(r.ip, 0), 2) AS k9,
    round(r.bb * 9.0 / NULLIF(r.ip, 0), 2) AS bb9,
    tr.season_ra9, tr.avg_ip_per_start, tr.blowup_pct, tr.ra_trend
FROM rates r LEFT JOIN trend tr ON true;
""".strip()


def _render_starter_column(col, label: str, name: str, row: dict) -> None:
    with col:
        st.markdown(f"**{label}: {name or '—'}**")
        st.metric("RA/9 (ERA proxy)", row.get("season_ra9", "—"))
        st.metric("K/9", row.get("k9", "—"))
        st.metric("BB/9", row.get("bb9", "—"))
        st.metric("IP / Start", row.get("avg_ip_per_start", "—"))
        trend = (row.get("ra_trend") or "flat")
        badge = {"down": "🟢 Improving", "up": "🔴 Worsening", "flat": "⚪ Stable"}.get(trend, "⚪ Stable")
        st.write(f"Trend: {badge}")
        st.metric("Blowup %", row.get("blowup_pct", "—"))


def render_matchup_tab() -> None:
    st.subheader("⚾ Matchup Duel & BVP Analyzer")
    st.caption("ERA is not in Statcast — figures shown as **RA/9** (Runs Allowed per 9). "
               "Metrics are empty until the backfill is loaded.")
    year = st.selectbox("Season", [2025, 2024, 2023, 2022, 2021], index=1)

    # 1) Starter Matchup Card -------------------------------------------------
    st.markdown("### 1 · Starter Duel")
    c1, c2 = st.columns(2)
    starter_a = c1.text_input("Starter A (name)", value="", placeholder="e.g. Cole, Gerrit")
    starter_b = c2.text_input("Starter B (name)", value="", placeholder="e.g. Ohtani, Shohei")
    if st.button("Compare starters", type="primary"):
        rows = {}
        err = None
        for key, nm in (("a", starter_a), ("b", starter_b)):
            if not nm:
                continue
            df, e = safe_query(_starter_card_query(nm, year))
            err = err or e
            rows[key] = df.iloc[0].to_dict() if not df.empty else {}
        if err:
            st.warning(f"Could not load starter data: {err}")
        col_a, col_b = st.columns(2)
        _render_starter_column(col_a, "Starter A", starter_a, rows.get("a", {}))
        _render_starter_column(col_b, "Starter B", starter_b, rows.get("b", {}))
        # Difference callouts using the pure helper.
        ra, rb = rows.get("a", {}), rows.get("b", {})
        k_adv = matchup.rate_advantage(ra.get("k9"), rb.get("k9"))
        bb_adv = matchup.rate_advantage(ra.get("bb9"), rb.get("bb9"))
        d1, d2 = st.columns(2)
        d1.metric("K/9 Advantage (A − B)", k_adv if k_adv is not None else "—")
        d2.metric("BB/9 Advantage (A − B)", bb_adv if bb_adv is not None else "—")

    # 2) BVP Head-to-Head Grid ------------------------------------------------
    st.markdown("### 2 · Batter vs. Pitcher Grid")
    bvp_pitcher = st.text_input("Starting pitcher (name)", value="", placeholder="e.g. Cole, Gerrit")
    if st.button("Load BVP grid"):
        sql = f"""
            SELECT batter, ab, h, hr, avg, obp, slg, ops
            FROM mv_bvp_matchups
            WHERE pitcher_name ILIKE '%{_esc(bvp_pitcher)}%'
            ORDER BY ab DESC LIMIT 25
        """.strip()
        df, err = safe_query(sql)
        if err:
            st.warning(f"Could not load BVP grid: {err}")
        elif df.empty:
            st.info("No batter-vs-pitcher rows (load the backfill, or check the name).")
        else:
            def _highlight(r):
                hot = matchup.should_highlight(r.get("ops"), int(r.get("ab") or 0))
                return ["background-color: #2b5d34" if hot else "" for _ in r]
            st.dataframe(df.style.apply(_highlight, axis=1), use_container_width=True, hide_index=True)
            st.caption("Highlight = OPS ≥ .900 or sample size ≥ 10 AB. "
                       "Rows with AB < 5 are small-sample — pair with the batter's career "
                       "split vs the pitcher's hand.")

    # 3) Bullpen & Platoon ----------------------------------------------------
    st.markdown("### 3 · Bullpen (L10) & Platoon")
    t1, t2 = st.columns(2)
    team_a = t1.text_input("Team A (abbr)", value="", placeholder="e.g. LAD")
    team_b = t2.text_input("Team B (abbr)", value="", placeholder="e.g. COL")
    if st.button("Compare bullpens & platoon"):
        teams = [t for t in (team_a, team_b) if t]
        if teams:
            in_list = ", ".join(f"'{_esc(t)}'" for t in teams)
            bull, e1 = safe_query(
                f"SELECT team, bullpen_ra9, whip, bullpen_ip, k9 FROM mv_bullpen_l10 "
                f"WHERE team IN ({in_list})"
            )
            plat, e2 = safe_query(
                f"SELECT team, ops_vs_rhp, ops_vs_lhp FROM v_team_platoon_ops "
                f"WHERE team IN ({in_list})"
            )
            if e1 or e2:
                st.warning(f"Could not load bullpen/platoon data: {e1 or e2}")
            st.markdown("**Bullpen — last 10 games (RA/9, WHIP, IP, K/9)**")
            st.dataframe(bull if not bull.empty else pd.DataFrame(
                columns=["team", "bullpen_ra9", "whip", "bullpen_ip", "k9"]),
                use_container_width=True, hide_index=True)
            if not bull.empty:
                fig = build_figure(bull, "bar",
                                   {"x": "team", "y": "bullpen_ra9", "title": "Bullpen RA/9 (L10)"})
                if fig is not None:
                    st.plotly_chart(fig, use_container_width=True)
            st.markdown("**Team OPS vs RHP / LHP**")
            st.dataframe(plat if not plat.empty else pd.DataFrame(
                columns=["team", "ops_vs_rhp", "ops_vs_lhp"]),
                use_container_width=True, hide_index=True)


# --------------------------------------------------------------------------- #
# App shell
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="MLB Data Intelligence", page_icon="⚾", layout="wide")
    st.title("⚾ MLB Data Intelligence")
    st.caption("Statcast 2021–2025 · Supabase PostgreSQL · Text-to-SQL agent")

    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    with st.sidebar:
        st.subheader("Recommended Queries")
        for q in RECOMMENDED_QUERIES:
            if st.button(q, use_container_width=True, key=f"rec_{q}"):
                st.session_state["pending_query"] = q
        st.divider()
        st.subheader("Matchup Deep-Dives")
        for q in MATCHUP_PRESETS:
            if st.button(q, use_container_width=True, key=f"preset_{q}"):
                st.session_state["pending_query"] = q
        st.caption("Deep-dive presets are answered by the agent in the Chat tab.")
        st.divider()
        st.subheader("Agent Engine")
        st.text(os.getenv("MLB_AGENT_MODEL", "gpt-4o"))

    # chat_input is pinned to the bottom of the app regardless of code position.
    prompt = st.chat_input("Ask a question about MLB Statcast data...")
    if prompt:
        st.session_state.pop("pending_query", None)
    else:
        prompt = st.session_state.pop("pending_query", None)

    if prompt:
        st.session_state["messages"].append({"role": "user", "content": prompt})
        with st.spinner("Generating SQL and querying Supabase..."):
            result = query_mlb_agent(prompt)
        st.session_state["messages"].append({"role": "assistant", "result": result})

    chat_tab, matchup_tab = st.tabs(["💬 Chat", "⚾ Matchup Duel & BVP Analyzer"])
    with chat_tab:
        render_chat_tab()
    with matchup_tab:
        render_matchup_tab()


if __name__ == "__main__":
    main()
