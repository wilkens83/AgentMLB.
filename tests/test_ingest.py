"""Unit tests for the ingestion transforms.

These cover the pure data-shaping logic that runs before any database call:
column filtering, NaN/NaT -> None normalization, numpy type coercion, and the
distinct-date extraction used for idempotent loads. No database or pybaseball
is required.
"""
import datetime

import numpy as np
import pandas as pd

import ingest


def _raw_frame():
    """A frame resembling a pybaseball Statcast pull, with extras and NaNs."""
    return pd.DataFrame(
        {
            "pitch_type": ["FF", "SL", None],
            "game_date": ["2024-05-05", "2024-05-05", "2024-08-18"],
            "release_speed": [95.1, np.nan, 88.0],
            "player_name": ["Ohtani, Shohei", "Cole, Gerrit", "Judge, Aaron"],
            "launch_speed": [110.4, np.nan, 108.4],
            "launch_speed_angle": [6, np.nan, 5],
            "at_bat_number": [1, 2, 3],
            # a column that is NOT in COLS and must be dropped:
            "some_unmapped_metric": [1.0, 2.0, 3.0],
        }
    )


def test_clean_dataframe_drops_unknown_columns():
    clean = ingest.clean_dataframe(_raw_frame())
    assert "some_unmapped_metric" not in clean.columns
    assert set(clean.columns).issubset(set(ingest.COLS))


def test_clean_dataframe_casts_game_date_to_date():
    clean = ingest.clean_dataframe(_raw_frame())
    assert isinstance(clean["game_date"].iloc[0], datetime.date)


def test_clean_dataframe_converts_nan_to_none():
    clean = ingest.clean_dataframe(_raw_frame())
    # release_speed row 1 was NaN -> None
    assert clean["release_speed"].iloc[1] is None
    assert clean["pitch_type"].iloc[2] is None


def test_dataframe_to_rows_coerces_numpy_scalars():
    clean = ingest.clean_dataframe(_raw_frame())
    rows = ingest.dataframe_to_rows(clean)
    assert len(rows) == 3
    for row in rows:
        for value in row:
            # No numpy scalar types should survive; psycopg2 can't adapt them.
            assert not isinstance(value, np.generic), f"leaked numpy type: {type(value)}"


def test_dataframe_to_rows_preserves_nulls_and_values():
    clean = ingest.clean_dataframe(_raw_frame())
    rows = ingest.dataframe_to_rows(clean)
    columns = list(clean.columns)
    rs_idx = columns.index("release_speed")
    # Row 0 has a real value, row 1 was NaN -> None.
    assert rows[0][rs_idx] == 95.1
    assert rows[1][rs_idx] is None


def test_game_dates_are_distinct_and_sorted():
    clean = ingest.clean_dataframe(_raw_frame())
    dates = ingest._game_dates(clean)
    assert dates == [datetime.date(2024, 5, 5), datetime.date(2024, 8, 18)]


def test_insert_empty_dataframe_is_noop():
    # Empty frame must not touch the database and returns 0.
    assert ingest.insert_dataframe(pd.DataFrame()) == 0
    assert ingest.batch_insert_df(pd.DataFrame()) == 0


def test_cols_list_has_no_duplicates():
    assert len(ingest.COLS) == len(set(ingest.COLS))
