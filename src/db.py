"""
src/db.py - Supabase / PostgreSQL connection pool, schema management, and
read-only query execution for the MLB Statcast analytics system.

Design notes
------------
* The connection pool is created lazily (see ``get_pool``) so that importing
  this module never requires ``DATABASE_URL`` to be set. This keeps unit tests
  and the pure helper functions (``assert_readonly_sql``, ``apply_row_limit``)
  runnable with no database present.
* ``execute_readonly_query`` is the ONLY sanctioned path for the Text-to-SQL
  agent. It validates that the statement is read-only, runs it inside a
  ``readonly`` transaction with a hard ``statement_timeout``, and caps the
  number of rows returned.
"""
from __future__ import annotations

import os
import re
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import pandas as pd

try:  # Load .env if python-dotenv is available; harmless if it is not.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional at runtime.
    pass

import psycopg2
import psycopg2.extensions
from psycopg2.pool import SimpleConnectionPool

# --------------------------------------------------------------------------- #
# Safety tunables (overridable via environment)
# --------------------------------------------------------------------------- #
STATEMENT_TIMEOUT_MS: int = int(os.getenv("STATEMENT_TIMEOUT_MS", "5000"))
MAX_ROWS: int = int(os.getenv("MAX_ROWS", "1000"))
DB_MAX_CONN: int = int(os.getenv("DB_MAX_CONN", "10"))

_pool: Optional[SimpleConnectionPool] = None
_pool_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# Read-only SQL validation (defense in depth alongside the agent's own checks)
# --------------------------------------------------------------------------- #
_READONLY_PREFIX_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"comment|copy|call|do|merge|vacuum|reindex|refresh|lock|set)\b",
    re.IGNORECASE,
)


def assert_readonly_sql(sql: str) -> str:
    """Validate that ``sql`` is a single read-only ``SELECT``/``WITH`` statement.

    Returns the trimmed statement (without a trailing semicolon) on success.
    Raises ``ValueError`` for anything that could mutate data or run multiple
    statements.
    """
    if not sql or not sql.strip():
        raise ValueError("Empty SQL statement.")

    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        raise ValueError("Multiple SQL statements are not allowed.")
    if not _READONLY_PREFIX_RE.match(stripped):
        raise ValueError("Only read-only SELECT/WITH queries are permitted.")
    if _FORBIDDEN_RE.search(stripped):
        raise ValueError("Query contains a forbidden (write/DDL) keyword.")
    return stripped


def apply_row_limit(sql: str, max_rows: int = MAX_ROWS) -> str:
    """Wrap ``sql`` in an outer ``LIMIT`` so it can never return more than
    ``max_rows`` rows.

    The statement is always wrapped, even when it already declares a ``LIMIT``:
    an inner ``LIMIT`` may be larger than ``max_rows`` or live inside a
    subquery, so an unconditional outer cap is the only hard guarantee. Any
    smaller inner ``LIMIT`` still takes effect inside the subquery.
    """
    return f"SELECT * FROM (\n{sql}\n) AS _capped LIMIT {int(max_rows)}"


# --------------------------------------------------------------------------- #
# Connection pool
# --------------------------------------------------------------------------- #
def get_database_url() -> str:
    """Return ``DATABASE_URL`` or raise a clear error if it is missing."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Export it or add it to a .env file, e.g. "
            "postgresql://postgres:[PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres"
        )
    return url


def get_pool() -> SimpleConnectionPool:
    """Lazily create and return the process-wide connection pool."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = SimpleConnectionPool(
                    minconn=1, maxconn=DB_MAX_CONN, dsn=get_database_url()
                )
    return _pool


@contextmanager
def get_db_connection() -> Iterator["psycopg2.extensions.connection"]:
    """Yield a pooled connection and always return it to the pool.

    Any half-open transaction is rolled back and read-only session flags are
    cleared before the connection is recycled, so a connection borrowed for a
    read cannot leave a later writer stuck in read-only mode.
    """
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
    finally:
        try:
            conn.rollback()
            conn.set_session(readonly=False, autocommit=False)
        except Exception:  # pragma: no cover - best-effort cleanup.
            pass
        pool.putconn(conn)


# --------------------------------------------------------------------------- #
# Schema management
# --------------------------------------------------------------------------- #
CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS statcast_pitches (
    id BIGSERIAL PRIMARY KEY,
    pitch_type VARCHAR(10),
    game_date DATE NOT NULL,
    release_speed DOUBLE PRECISION,
    release_pos_x DOUBLE PRECISION,
    release_pos_z DOUBLE PRECISION,
    player_name VARCHAR(100),
    batter BIGINT,
    pitcher BIGINT,
    events VARCHAR(50),
    description VARCHAR(100),
    zone INT,
    stand VARCHAR(5),
    p_throws VARCHAR(5),
    home_team VARCHAR(10),
    away_team VARCHAR(10),
    type VARCHAR(5),
    hit_location INT,
    bb_type VARCHAR(50),
    balls INT,
    strikes INT,
    game_year INT,
    pfx_x DOUBLE PRECISION,
    pfx_z DOUBLE PRECISION,
    plate_x DOUBLE PRECISION,
    plate_z DOUBLE PRECISION,
    launch_speed DOUBLE PRECISION,
    launch_angle DOUBLE PRECISION,
    effective_speed DOUBLE PRECISION,
    release_spin_rate DOUBLE PRECISION,
    release_extension DOUBLE PRECISION,
    game_pk BIGINT,
    pitch_name VARCHAR(50),
    home_score INT,
    away_score INT,
    bat_score INT,
    fld_score INT,
    post_bat_score INT,
    post_fld_score INT,
    estimated_ba_using_speedangle DOUBLE PRECISION,
    estimated_woba_using_speedangle DOUBLE PRECISION,
    woba_value DOUBLE PRECISION,
    woba_denom DOUBLE PRECISION,
    babip_value DOUBLE PRECISION,
    iso_value DOUBLE PRECISION,
    launch_speed_angle INT,
    at_bat_number INT,
    pitch_number INT
);

CREATE INDEX IF NOT EXISTS idx_statcast_date ON statcast_pitches(game_date);
CREATE INDEX IF NOT EXISTS idx_statcast_player ON statcast_pitches(player_name);
CREATE INDEX IF NOT EXISTS idx_statcast_pitch_type ON statcast_pitches(pitch_type);
CREATE INDEX IF NOT EXISTS idx_statcast_year ON statcast_pitches(game_year);
"""


def init_supabase_schema() -> None:
    """Create the ``statcast_pitches`` table and its indexes (idempotent)."""
    with get_db_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
            conn.commit()
            print("Supabase schema initialized successfully.")
        except Exception:
            conn.rollback()
            raise


# --------------------------------------------------------------------------- #
# Read-only query execution
# --------------------------------------------------------------------------- #
def execute_readonly_query(sql: str, max_rows: int = MAX_ROWS) -> pd.DataFrame:
    """Validate and execute a read-only query, returning a capped DataFrame.

    * Rejects any non-``SELECT``/``WITH`` statement.
    * Runs inside a ``readonly`` transaction with ``statement_timeout``.
    * Never returns more than ``max_rows`` rows.
    """
    clean = assert_readonly_sql(sql)
    wrapped = apply_row_limit(clean, max_rows)

    # get_db_connection() rolls back and clears the read-only flag on exit
    # (success or error), so there is no session cleanup to do here.
    with get_db_connection() as conn:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = %s", (int(STATEMENT_TIMEOUT_MS),))
            cur.execute(wrapped)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = cur.fetchall()
        return pd.DataFrame(rows, columns=columns)
