"""
scripts/backfill_5_years.py - Resilient 5-Year Statcast Backfill with Checkpoints.

Initializes the schema (via scripts/init_supabase.sql), then ingests seasons
2021-2025 in 5-day chunks from March to November using
``psycopg2.extras.execute_values`` (batch size 2000). A per-chunk row is written
to ``ingest_checkpoints`` so the job is fully resumable: stop and restart at any
time without re-downloading or duplicating data. On completion it refreshes both
materialized views and runs ``VACUUM ANALYZE`` to optimize the query planner.

Run:
    python scripts/backfill_5_years.py
    python scripts/backfill_5_years.py --years 2024 2025 --chunk-days 5
"""
from __future__ import annotations

import argparse
import datetime
import os
import time
from typing import Any, List

import pandas as pd
import psycopg2
import psycopg2.extensions
import psycopg2.extras

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set in .env")

# Supabase requires TLS; default to 'require' unless overridden.
PGSSLMODE = os.getenv("PGSSLMODE", "require")

HERE = os.path.dirname(os.path.abspath(__file__))
INIT_SQL_PATH = os.path.join(HERE, "init_supabase.sql")

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


def connect() -> "psycopg2.extensions.connection":
    """Open a psycopg2 connection to Supabase (TLS enforced)."""
    return psycopg2.connect(DATABASE_URL, sslmode=PGSSLMODE)


def initialize_schema(conn) -> None:
    """Run scripts/init_supabase.sql to create tables, indexes, and views."""
    with open(INIT_SQL_PATH, "r", encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("Schema initialized (tables, indexes, checkpoints, materialized views).")


def is_chunk_completed(conn, start_date, end_date) -> bool:
    """Return True if this date chunk has already been ingested."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ingest_checkpoints WHERE start_date = %s AND end_date = %s",
            (start_date, end_date),
        )
        return cur.fetchone() is not None


def record_checkpoint(conn, start_date, end_date, pitch_count: int) -> None:
    """Record a completed date chunk (idempotent via ON CONFLICT)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_checkpoints (start_date, end_date, pitch_count)
            VALUES (%s, %s, %s)
            ON CONFLICT (start_date, end_date) DO NOTHING;
            """,
            (start_date, end_date, pitch_count),
        )
    conn.commit()


def _to_pg_value(value: Any) -> Any:
    """Coerce a cell into a psycopg2-adaptable value.

    Unboxes numpy scalars (numpy.int64/float64/bool_ are not natively
    adaptable) and normalizes NaN/NaT to None (SQL NULL).
    """
    if value is None:
        return None
    item = value.item() if hasattr(value, "item") else value
    if item is None:
        return None
    if not isinstance(item, (str, bytes)):
        try:
            if pd.isna(item):
                return None
        except (TypeError, ValueError):  # pragma: no cover
            pass
    return item


def batch_insert(conn, df: pd.DataFrame) -> int:
    """High-speed, numpy-safe batch insert into ``statcast_pitches``.

    Deduplication is handled by the ``ingest_checkpoints`` table rather than by
    deleting rows here: a chunk is fetched and inserted at most once, and a
    completed chunk is skipped on any later run. Returns the number of rows
    inserted.
    """
    if df is None or df.empty:
        return 0

    available = [c for c in COLS if c in df.columns]
    clean = df[available].copy()
    clean["game_date"] = pd.to_datetime(clean["game_date"], errors="coerce").dt.date
    # Drop rows whose game_date is invalid (DATE NOT NULL column).
    clean = clean.dropna(subset=["game_date"])
    if clean.empty:
        return 0
    clean = clean.astype(object)

    insert_sql = f"INSERT INTO statcast_pitches ({', '.join(available)}) VALUES %s"
    values = [tuple(_to_pg_value(v) for v in row) for row in clean.to_numpy()]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, insert_sql, values, page_size=BATCH_SIZE)
    conn.commit()
    return len(values)


def refresh_views_and_optimize(conn) -> None:
    """Refresh materialized views and VACUUM ANALYZE the base table."""
    print("Refreshing materialized views (mv_pitcher_arsenals, mv_batter_barrels)...")
    with conn.cursor() as cur:
        cur.execute("REFRESH MATERIALIZED VIEW mv_pitcher_arsenals;")
        cur.execute("REFRESH MATERIALIZED VIEW mv_batter_barrels;")
    conn.commit()

    # VACUUM cannot run inside a transaction block; switch to autocommit.
    print("Running VACUUM ANALYZE statcast_pitches...")
    old_isolation = conn.isolation_level
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute("VACUUM ANALYZE statcast_pitches;")
    finally:
        conn.set_isolation_level(old_isolation)


def backfill_5_years(years: List[int] | None = None, chunk_days: int = 5) -> int:
    """Ingest the given seasons (default 2021-2025) in resumable chunks."""
    years = years or [2021, 2022, 2023, 2024, 2025]

    # Lazy import: only needed when actually fetching data.
    from pybaseball import statcast

    conn = connect()
    total_ingested = 0
    try:
        print("=" * 60)
        print(f"Starting Statcast ingestion for seasons: {years}")
        print("=" * 60)
        initialize_schema(conn)

        for year in years:
            start_date = datetime.date(year, 3, 20)
            end_date = datetime.date(year, 11, 5)
            current = start_date
            print(f"\nIngesting Season {year}...")

            while current <= end_date:
                chunk_end = min(current + datetime.timedelta(days=chunk_days), end_date)

                if is_chunk_completed(conn, current, chunk_end):
                    print(f"  [skip] {current}..{chunk_end} already ingested.")
                    current = chunk_end + datetime.timedelta(days=1)
                    continue

                print(f"  [fetch] {current}..{chunk_end} ...", end="", flush=True)
                try:
                    df = statcast(str(current), str(chunk_end))
                    if df is not None and not df.empty:
                        count = batch_insert(conn, df)
                        record_checkpoint(conn, current, chunk_end, count)
                        total_ingested += count
                        print(f" done ({count:,} pitches).")
                    else:
                        record_checkpoint(conn, current, chunk_end, 0)
                        print(" no games.")
                except Exception as exc:  # noqa: BLE001 - keep the backfill going
                    conn.rollback()
                    print(f" ERROR: {exc}")
                    time.sleep(2)

                current = chunk_end + datetime.timedelta(days=1)

        print("\n" + "=" * 60)
        print(f"Backfill complete. New pitches ingested this run: {total_ingested:,}")
        refresh_views_and_optimize(conn)
        print("Supabase is initialized, indexed, and ready for analytics.")
    finally:
        conn.close()
    return total_ingested


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resilient 5-year Statcast backfill")
    parser.add_argument(
        "--years", type=int, nargs="+", default=None,
        help="Seasons to ingest (default: 2021 2022 2023 2024 2025)",
    )
    parser.add_argument("--chunk-days", type=int, default=5)
    args = parser.parse_args()
    backfill_5_years(years=args.years, chunk_days=args.chunk_days)
