"""Unit tests for the Text-to-SQL agent and read-only SQL safety layer.

These tests exercise pure logic only: SQL validation, row-limit wrapping,
LLM-response parsing, chart-type normalization, and Plotly figure building.
No database, LLM, or network is required.
"""
import pandas as pd
import pytest

import db
import agent


# --------------------------------------------------------------------------- #
# Read-only SQL validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM statcast_pitches",
        "  select player_name from statcast_pitches limit 10 ",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT count(*) FROM statcast_pitches WHERE player_name ILIKE '%Ohtani%';",
    ],
)
def test_assert_readonly_accepts_selects(sql):
    assert db.assert_readonly_sql(sql)  # returns trimmed statement, truthy


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO statcast_pitches (id) VALUES (1)",
        "UPDATE statcast_pitches SET events = 'x'",
        "DELETE FROM statcast_pitches",
        "DROP TABLE statcast_pitches",
        "ALTER TABLE statcast_pitches ADD COLUMN c INT",
        "TRUNCATE statcast_pitches",
        "SELECT 1; DROP TABLE statcast_pitches",  # multi-statement
        "CREATE TABLE t (a int)",
        "",
        "   ",
    ],
)
def test_assert_readonly_rejects_writes(sql):
    with pytest.raises(ValueError):
        db.assert_readonly_sql(sql)


def test_assert_readonly_allows_columns_that_contain_keywords():
    # Column names embedding keywords must not trip the word-boundary regex.
    sql = "SELECT created_at, update_time, insert_id FROM some_view"
    assert db.assert_readonly_sql(sql)


# --------------------------------------------------------------------------- #
# Row-limit wrapping
# --------------------------------------------------------------------------- #
def test_apply_row_limit_wraps_when_absent():
    out = db.apply_row_limit("SELECT a FROM t", max_rows=500)
    assert "LIMIT 500" in out
    assert "_capped" in out


def test_apply_row_limit_always_caps_even_with_inner_limit():
    # A larger inner LIMIT must not defeat the hard outer cap.
    sql = "SELECT a FROM t LIMIT 5000000"
    out = db.apply_row_limit(sql, max_rows=1000)
    assert "_capped" in out
    assert out.rstrip().endswith("LIMIT 1000")
    # The inner query (with its own LIMIT) is preserved inside the wrapper.
    assert "LIMIT 5000000" in out


# --------------------------------------------------------------------------- #
# LLM response parsing
# --------------------------------------------------------------------------- #
def test_parse_agent_json_plain():
    payload = '{"sql": "SELECT 1", "chart_type": "bar"}'
    parsed = agent.parse_agent_json(payload)
    assert parsed["sql"] == "SELECT 1"


def test_parse_agent_json_with_code_fence():
    payload = '```json\n{"sql": "SELECT 1", "explanation": "x"}\n```'
    parsed = agent.parse_agent_json(payload)
    assert parsed["sql"] == "SELECT 1"


def test_parse_agent_json_embedded_object():
    payload = 'Here is the query: {"sql": "SELECT 1"} hope it helps'
    parsed = agent.parse_agent_json(payload)
    assert parsed["sql"] == "SELECT 1"


def test_parse_agent_json_invalid_raises():
    with pytest.raises(ValueError):
        agent.parse_agent_json("not json at all")


# --------------------------------------------------------------------------- #
# Chart type normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("scatter", "scatter"),
        ("BAR", "bar"),
        ("pie", "none"),
        (None, "none"),
        ("", "none"),
    ],
)
def test_normalize_chart_type(raw, expected):
    assert agent.normalize_chart_type(raw) == expected


# --------------------------------------------------------------------------- #
# Plotly figure building
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sample_df():
    return pd.DataFrame(
        {
            "launch_speed": [108.4, 111.2, 115.3],
            "launch_angle": [29.2, 31.4, 27.0],
            "pitch_name": ["Sinker", "Slider", "4-Seam Fastball"],
            "plate_x": [0.1, -0.2, 0.3],
            "plate_z": [2.5, 3.0, 2.1],
        }
    )


def test_build_figure_scatter(sample_df):
    fig = agent.build_figure(
        sample_df,
        "scatter",
        {"x": "launch_speed", "y": "launch_angle", "color": "pitch_name"},
    )
    assert fig is not None
    assert len(fig.data) >= 1


def test_build_figure_histogram(sample_df):
    fig = agent.build_figure(sample_df, "histogram", {"x": "launch_speed"})
    assert fig is not None


def test_build_figure_heatmap_defaults_to_plate_coords(sample_df):
    fig = agent.build_figure(sample_df, "heatmap", {})
    assert fig is not None


def test_build_figure_none_type_returns_none(sample_df):
    assert agent.build_figure(sample_df, "none", {}) is None


def test_build_figure_missing_columns_returns_none(sample_df):
    # Requested columns don't exist -> graceful fallback (None), no exception.
    assert agent.build_figure(sample_df, "scatter", {"x": "nope", "y": "nada"}) is None


def test_build_figure_empty_df_returns_none():
    assert agent.build_figure(pd.DataFrame(), "scatter", {"x": "a", "y": "b"}) is None


def test_build_figure_ignores_nonexistent_color(sample_df):
    # color column absent should not crash; figure still builds.
    fig = agent.build_figure(
        sample_df, "scatter", {"x": "launch_speed", "y": "launch_angle", "color": "ghost"}
    )
    assert fig is not None
