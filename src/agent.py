"""
src/agent.py - PostgreSQL Text-to-SQL agent and Plotly visualization builder.

The agent turns a natural-language question into a validated, read-only
PostgreSQL query against ``statcast_pitches``, executes it via the sanctioned
``db.execute_readonly_query`` path, and produces a Plotly figure spec.

The LLM call (via litellm) is imported lazily so that the pure helpers used by
the UI and tests -- SQL extraction/validation, figure building, CSV export --
work with no network access and no LLM dependency installed.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, Optional

import pandas as pd

# Make ``from db import ...`` resolve regardless of how this module is loaded.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import assert_readonly_sql, execute_readonly_query  # noqa: E402

DEFAULT_MODEL = os.getenv("MLB_AGENT_MODEL", "gpt-4o")

SYSTEM_PROMPT = """
You are an expert MLB Data Analyst generating PostgreSQL queries for a Supabase database.
Table: `statcast_pitches`

PostgreSQL Rules:
1. Always generate valid PostgreSQL syntax (e.g., ILIKE, DATE_TRUNC, EXTRACT).
2. Append `LIMIT 1000` to non-aggregate queries.
3. Only output SELECT statements. No DDL or DML.
4. Pitch codes: FF (4-Seam), SL (Slider), CH (Changeup), CU (Curveball), SI (Sinker), FC (Cutter), ST (Sweeper).
5. Contact quality: `launch_speed_angle = 6` denotes a Barrel.
6. Use ILIKE '%Lastname%' for case-insensitive player-name matching.

Key columns: pitch_type, pitch_name, game_date, game_year, player_name,
release_speed, release_spin_rate, launch_speed, launch_angle, launch_speed_angle,
plate_x, plate_z, events, description, balls, strikes.

Output JSON ONLY (no markdown fences), with this exact shape:
{
  "sql": "SELECT ...",
  "explanation": "Short summary of the analysis.",
  "chart_type": "scatter | bar | line | histogram | heatmap | none",
  "chart_config": {
    "x": "column_name",
    "y": "column_name",
    "color": "column_name_or_null",
    "title": "Chart Title"
  }
}
""".strip()

VALID_CHART_TYPES = {"scatter", "bar", "line", "histogram", "heatmap", "none"}


# --------------------------------------------------------------------------- #
# Parsing helpers (pure, no network)
# --------------------------------------------------------------------------- #
def parse_agent_json(content: str) -> Dict[str, Any]:
    """Parse the LLM response into a dict, tolerating markdown code fences.

    Raises ``ValueError`` if no JSON object can be recovered.
    """
    if not content or not content.strip():
        raise ValueError("Empty LLM response.")

    text = content.strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the first {...} block in the string.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain a JSON object.")
        return json.loads(match.group(0))


def normalize_chart_type(chart_type: Optional[str]) -> str:
    """Return a supported chart type, defaulting unknown values to 'none'."""
    if not chart_type:
        return "none"
    ct = str(chart_type).strip().lower()
    return ct if ct in VALID_CHART_TYPES else "none"


# --------------------------------------------------------------------------- #
# Visualization (pure, no network)
# --------------------------------------------------------------------------- #
def build_figure(df: pd.DataFrame, chart_type: str, chart_config: Dict[str, Any]):
    """Build a Plotly figure for ``df`` or return ``None`` if not chartable.

    Gracefully returns ``None`` when the DataFrame is empty or the requested
    x/y columns are absent, so the UI can fall back to a plain table.
    """
    import plotly.express as px  # local import; only needed when charting

    chart_type = normalize_chart_type(chart_type)
    if chart_type == "none" or df is None or df.empty:
        return None

    cfg = chart_config or {}
    x = cfg.get("x")
    y = cfg.get("y")
    color = cfg.get("color") or None
    title = cfg.get("title") or "MLB Statcast Analysis"

    # Only keep a color column that actually exists.
    if color not in df.columns:
        color = None

    def _has(col: Optional[str]) -> bool:
        return bool(col) and col in df.columns

    try:
        if chart_type == "scatter":
            if not (_has(x) and _has(y)):
                return None
            return px.scatter(df, x=x, y=y, color=color, title=title)
        if chart_type == "bar":
            if not (_has(x) and _has(y)):
                return None
            return px.bar(df, x=x, y=y, color=color, title=title)
        if chart_type == "line":
            if not (_has(x) and _has(y)):
                return None
            return px.line(df, x=x, y=y, color=color, title=title)
        if chart_type == "histogram":
            if not _has(x):
                return None
            return px.histogram(df, x=x, color=color, title=title)
        if chart_type == "heatmap":
            # Strike-zone style density heatmap over plate_x / plate_z.
            hx = x if _has(x) else ("plate_x" if _has("plate_x") else None)
            hy = y if _has(y) else ("plate_z" if _has("plate_z") else None)
            if not (hx and hy):
                return None
            return px.density_heatmap(
                df, x=hx, y=hy, nbinsx=20, nbinsy=20, title=title
            )
    except Exception:
        # Never let a plotting error break the request; fall back to a table.
        return None
    return None


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    """Encode a DataFrame as UTF-8 CSV bytes for a download button."""
    if df is None:
        df = pd.DataFrame()
    return df.to_csv(index=False).encode("utf-8")


# --------------------------------------------------------------------------- #
# LLM call
# --------------------------------------------------------------------------- #
def generate_sql(user_query: str, model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """Call the LLM to produce a validated SQL spec (no execution).

    Returns a dict with keys: ``sql``, ``explanation``, ``chart_type``,
    ``chart_config``. Raises on network/parse/validation failure.
    """
    import litellm  # heavy/optional import; only needed for a live query

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"User Request: {user_query}"},
    ]
    response = litellm.completion(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.1,
    )
    parsed = parse_agent_json(response.choices[0].message.content)

    sql = assert_readonly_sql(parsed.get("sql", ""))
    return {
        "sql": sql,
        "explanation": parsed.get("explanation", ""),
        "chart_type": normalize_chart_type(parsed.get("chart_type")),
        "chart_config": parsed.get("chart_config", {}) or {},
    }


def query_mlb_agent(user_query: str, model: str = DEFAULT_MODEL) -> Dict[str, Any]:
    """End-to-end: NL question -> SQL -> executed DataFrame -> figure spec.

    Always returns a dict. On failure the ``error`` key is populated and
    ``data`` is an empty DataFrame, so callers (the UI) can render gracefully
    without try/except at the call site.
    """
    result: Dict[str, Any] = {
        "sql": "",
        "explanation": "",
        "chart_type": "none",
        "chart_config": {},
        "data": pd.DataFrame(),
        "error": None,
    }
    try:
        spec = generate_sql(user_query, model=model)
        result.update(spec)
        result["data"] = execute_readonly_query(spec["sql"])
    except Exception as exc:  # noqa: BLE001 - surface as data, not a crash
        result["error"] = str(exc)
    return result
