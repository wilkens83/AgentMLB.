"""
src/app.py - Streamlit chat dashboard for the MLB Statcast analytics system.

Users ask questions in natural language; the Text-to-SQL agent generates a
read-only PostgreSQL query against the Supabase ``statcast_pitches`` table,
executes it, and the result is rendered as a Plotly chart plus a downloadable
table. The raw generated SQL is available in an expander for transparency.

Run with:  ``streamlit run src/app.py``
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import streamlit as st

# Make ``from agent import ...`` resolve when run as ``streamlit run src/app.py``.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def main() -> None:
    st.set_page_config(page_title="MLB Data Intelligence", page_icon="⚾", layout="wide")
    st.title("⚾ MLB Data Intelligence")
    st.caption("Statcast 2021–2025 · Supabase PostgreSQL · Text-to-SQL agent")

    with st.sidebar:
        st.subheader("Recommended Queries")
        for q in RECOMMENDED_QUERIES:
            if st.button(q, use_container_width=True):
                st.session_state["pending_query"] = q
        st.divider()
        st.subheader("Agent Engine")
        st.text(os.getenv("MLB_AGENT_MODEL", "gpt-4o"))

    if "messages" not in st.session_state:
        st.session_state["messages"] = []

    # Replay prior conversation.
    for msg in st.session_state["messages"]:
        with st.chat_message(msg["role"]):
            if msg["role"] == "user":
                st.markdown(msg["content"])
            else:
                render_result(msg["result"])

    # Typed input takes priority; otherwise use a query queued by a sidebar button.
    prompt = st.chat_input("Ask a question about MLB Statcast data...")
    if prompt:
        st.session_state.pop("pending_query", None)
    else:
        prompt = st.session_state.pop("pending_query", None)

    if prompt:
        st.session_state["messages"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        with st.chat_message("assistant"):
            with st.spinner("Generating SQL and querying Supabase..."):
                result = query_mlb_agent(prompt)
            render_result(result)
        st.session_state["messages"].append({"role": "assistant", "result": result})


if __name__ == "__main__":
    main()
