"""Tests for scripts/backfill_5_years.py helpers and the init SQL contract.

No database is touched: a dummy DATABASE_URL lets the module import, and only
pure helpers + the SQL file's declared objects are checked.
"""
import importlib.util
import os

import numpy as np
import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "backfill_5_years.py")
INIT_SQL = os.path.join(ROOT, "scripts", "init_supabase.sql")


@pytest.fixture(scope="module")
def bf():
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy")
    spec = importlib.util.spec_from_file_location("backfill_5_years", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_to_pg_value_unboxes_numpy(bf):
    assert bf._to_pg_value(np.int64(5)) == 5
    assert not isinstance(bf._to_pg_value(np.int64(5)), np.generic)
    assert bf._to_pg_value(np.float64(3.5)) == 3.5
    assert not isinstance(bf._to_pg_value(np.float64(3.5)), np.generic)


def test_to_pg_value_nulls(bf):
    assert bf._to_pg_value(np.nan) is None
    assert bf._to_pg_value(pd.NaT) is None
    assert bf._to_pg_value(None) is None


def test_to_pg_value_passthrough(bf):
    assert bf._to_pg_value("FF") == "FF"
    assert bf._to_pg_value(42) == 42


def test_cols_unique(bf):
    assert len(bf.COLS) == len(set(bf.COLS))


def test_batch_size_is_2000(bf):
    assert bf.BATCH_SIZE == 2000


# --------------------------------------------------------------------------- #
# init_supabase.sql contract
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def sql_text():
    with open(INIT_SQL, "r", encoding="utf-8") as fh:
        return fh.read().lower()


def test_sql_creates_core_objects(sql_text):
    assert "create table if not exists statcast_pitches" in sql_text
    assert "create table if not exists ingest_checkpoints" in sql_text
    assert "create materialized view if not exists mv_pitcher_arsenals" in sql_text
    assert "create materialized view if not exists mv_batter_barrels" in sql_text


@pytest.mark.parametrize(
    "column",
    ["game_date", "game_year", "player_name", "pitch_type", "events", "launch_speed_angle"],
)
def test_sql_indexes_required_columns(sql_text, column):
    # Each required column must appear in at least one CREATE INDEX statement.
    assert f"on statcast_pitches({column}" in sql_text or f"({column})" in sql_text
