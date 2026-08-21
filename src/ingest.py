"""
src/ingest.py - High-throughput batch ingestion of MLB Statcast data into
Supabase / PostgreSQL.

Statcast produces hundreds of thousands of rows per season, so inserts use
``psycopg2.extras.execute_values`` (multi-row VALUES) which is dramatically
faster than row-by-row inserts and avoids HTTP timeouts.

Idempotency
-----------
There is no natural unique key on a raw Statcast pitch, so re-running a
backfill would otherwise duplicate rows. ``insert_dataframe`` therefore deletes
any existing rows for the game dates it is about to load (within the same
transaction) before inserting, making every load idempotent by ``game_date``.
"""
from __future__ import annotations

import datetime
import os
import sys
from typing import Iterable, List, Optional

import pandas as pd

# Make ``from db import ...`` work whether this module is imported as
# ``src.ingest`` (from the repo root) or as ``ingest`` (with src on the path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_db_connection, init_supabase_schema  # noqa: E402

# Columns we persist, in table order. Any not present in a given Statcast pull
# are simply skipped (schema drift tolerance).
COLS: List[str] = [
    "pitch_type", "game_date", "release_speed", "release_pos_x", "release_pos_z",
    "player_name", "batter", "pitcher", "events", "description", "zone", "stand",
    "p_throws", "home_team", "away_team", "type", "hit_location", "bb_type",
    "balls", "strikes", "game_year", "pfx_x", "pfx_z", "plate_x", "plate_z",
    "launch_speed", "launch_angle", "effective_speed", "release_spin_rate",
    "release_extension", "game_pk", "pitch_name", "home_score", "away_score",
    "bat_score", "fld_score", "post_bat_score", "post_fld_score",
    "estimated_ba_using_speedangle", "estimated_woba_using_speedangle",
    "woba_value", "woba_denom", "babip_value", "iso_value",
    "launch_speed_angle", "at_bat_number", "pitch_number",
    "inning", "outs_when_up", "inning_topbot",
]

BATCH_SIZE = 2000


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` restricted to known columns and normalized.

    * Keeps only columns present in both ``df`` and ``COLS``.
    * Casts ``game_date`` to a plain ``date``.
    * Converts pandas/numpy NaN/NaT to ``None`` so psycopg2 writes SQL NULL.
    """
    available = [c for c in COLS if c in df.columns]
    clean = df[available].copy()
    if "game_date" in clean.columns:
        clean["game_date"] = pd.to_datetime(clean["game_date"], errors="coerce").dt.date
    # Replace all missing values with None (object dtype). Mask on the cleaned
    # frame so a date coerced to NaT above is caught (not the original frame).
    clean = clean.astype(object)
    clean = clean.where(pd.notnull(clean), None)
    return clean


def _to_pg_value(value):
    """Coerce a single cell into something psycopg2 can adapt.

    Handles numpy scalar types (``numpy.int64``/``float64``/``bool_``) which
    psycopg2 cannot adapt natively, and normalizes NaN/NaT to ``None``.
    """
    if value is None:
        return None
    # numpy scalars expose ``.item()`` to yield a native Python scalar.
    item = value.item() if hasattr(value, "item") else value
    if item is None:
        return None
    # Treat any pandas-recognized missing value (NaN, NaT) as SQL NULL, but
    # never run pd.isna on strings/bytes (it is unnecessary and can be noisy).
    if not isinstance(item, (str, bytes)):
        try:
            if pd.isna(item):
                return None
        except (TypeError, ValueError):  # pragma: no cover - unhashable/array
            pass
    return item


def dataframe_to_rows(df: pd.DataFrame) -> List[tuple]:
    """Convert a cleaned DataFrame into a list of psycopg2-ready tuples."""
    return [tuple(_to_pg_value(v) for v in row) for row in df.to_numpy()]


def _game_dates(df: pd.DataFrame) -> List[datetime.date]:
    """Return the sorted set of distinct game dates present in ``df``."""
    if "game_date" not in df.columns:
        return []
    dates = {d for d in df["game_date"].tolist() if d is not None}
    return sorted(dates)


def insert_dataframe(df: pd.DataFrame, replace_existing: bool = True) -> int:
    """Bulk-insert a Statcast DataFrame into ``statcast_pitches``.

    When ``replace_existing`` is True (default) any rows already stored for the
    game dates in ``df`` are deleted first, making the load idempotent.

    Returns the number of rows inserted.
    """
    if df is None or df.empty:
        return 0

    import psycopg2.extras  # local import keeps module import light

    clean = clean_dataframe(df)
    if clean.empty:
        return 0

    columns = list(clean.columns)
    rows = dataframe_to_rows(clean)
    dates = _game_dates(clean) if replace_existing else []

    insert_sql = f"INSERT INTO statcast_pitches ({', '.join(columns)}) VALUES %s"

    with get_db_connection() as conn:
        try:
            with conn.cursor() as cur:
                if dates:
                    cur.execute(
                        "DELETE FROM statcast_pitches WHERE game_date = ANY(%s)",
                        (dates,),
                    )
                psycopg2.extras.execute_values(
                    cur, insert_sql, rows, page_size=BATCH_SIZE
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return len(rows)


# Backwards-compatible alias matching the original spec name.
def batch_insert_df(df: pd.DataFrame) -> int:
    """Alias for :func:`insert_dataframe` (idempotent bulk insert)."""
    return insert_dataframe(df, replace_existing=True)


def _fetch_statcast(start: str, end: str) -> Optional[pd.DataFrame]:
    """Fetch Statcast data for a date range (lazy import of pybaseball)."""
    from pybaseball import statcast  # heavy import; only needed at ingest time

    return statcast(start_dt=start, end_dt=end)


def backfill_statcast(
    start_year: int, end_year: int, chunk_days: int = 5
) -> int:
    """Backfill one or more seasons into Supabase in small date chunks.

    Chunking keeps each ``pybaseball`` request small (kinder to the source and
    resilient to timeouts). Errors on a single chunk are logged and skipped so
    a long backfill is not aborted by one bad range. Returns total rows loaded.
    """
    init_supabase_schema()
    start_date = datetime.date(start_year, 3, 20)
    end_date = datetime.date(end_year, 11, 5)

    total = 0
    current = start_date
    while current <= end_date:
        chunk_end = min(current + datetime.timedelta(days=chunk_days), end_date)
        print(f"Ingesting into Supabase: {current} to {chunk_end}...")
        try:
            df = _fetch_statcast(str(current), str(chunk_end))
            if df is not None and not df.empty:
                total += insert_dataframe(df, replace_existing=True)
        except Exception as exc:  # noqa: BLE001 - keep the backfill going
            print(f"Error on chunk {current} to {chunk_end}: {exc}")
        current = chunk_end + datetime.timedelta(days=1)

    print(f"Backfill complete. Inserted {total} pitches for {start_year}-{end_year}.")
    return total


def date_has_data(day: datetime.date | str) -> bool:
    """Return True if any pitches are already stored for ``day``."""
    if isinstance(day, str):
        day = datetime.date.fromisoformat(day)
    with get_db_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM statcast_pitches WHERE game_date = %s LIMIT 1",
                    (day,),
                )
                return cur.fetchone() is not None
        except Exception:
            conn.rollback()
            raise


def run_daily_update() -> int:
    """Append yesterday's completed games, skipping if already present.

    Returns the number of rows inserted (0 if the day was already loaded or
    no games were played).
    """
    yesterday = datetime.date.today() - datetime.timedelta(days=1)

    if date_has_data(yesterday):
        print(f"Data for {yesterday} already in Supabase. Skipping.")
        return 0

    print(f"Fetching daily Statcast for {yesterday}...")
    df = _fetch_statcast(str(yesterday), str(yesterday))
    if df is None or df.empty:
        print(f"No Statcast rows returned for {yesterday}.")
        return 0

    inserted = insert_dataframe(df, replace_existing=True)
    print(f"Inserted {inserted} pitches for {yesterday} into Supabase.")
    return inserted


if __name__ == "__main__":  # pragma: no cover - CLI convenience
    import argparse

    parser = argparse.ArgumentParser(description="MLB Statcast ingestion")
    sub = parser.add_subparsers(dest="cmd", required=True)

    bf = sub.add_parser("backfill", help="Backfill seasons")
    bf.add_argument("start_year", type=int)
    bf.add_argument("end_year", type=int)
    bf.add_argument("--chunk-days", type=int, default=5)

    sub.add_parser("daily", help="Append yesterday's games")

    args = parser.parse_args()
    if args.cmd == "backfill":
        backfill_statcast(args.start_year, args.end_year, args.chunk_days)
    elif args.cmd == "daily":
        run_daily_update()
