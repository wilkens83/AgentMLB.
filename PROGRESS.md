# PROGRESS — MLB Analytics System (Supabase)

## Files created / modified
- `CLAUDE.md` — agent spec (Supabase/PostgreSQL dialect, Statcast dictionary, checkpoint rules)
- `src/db.py` — lazy PG connection pool, `statcast_pitches` schema + indexes, read-only query execution (SELECT/WITH-only validation, `statement_timeout`, hard outer row cap)
- `src/ingest.py` — `execute_values` bulk insert (2000/page), chunked `backfill_statcast`, idempotent `run_daily_update` (dedup by `game_date`), NaN/NaT→NULL + numpy→PG coercion
- `src/agent.py` — litellm Text-to-SQL (lazy import), robust JSON parsing (code-fence tolerant), read-only guard before execution, Plotly builder (scatter/bar/line/histogram/strike-zone heatmap), CSV export
- `src/app.py` — Streamlit chat UI: SQL expander, Plotly render, CSV download, empty/error handling
- `workflows/build_system.yml` — diamond-pattern Supabase build graph
- `workflows/daily_pipeline.yml` — daily ingestion + verification graph (cron 04:00 ET)
- `loops/sql_verification_loop.md`, `loops/code_quality_loop.md` — self-grading loops
- `tests/` — `test_sql_agent.py`, `test_ingest.py`, `test_ui_components.py`, `conftest.py`
- `tests/audit_sql.md`, `tests/audit_ingest.md` — independent checker reports
- `run.sh` — env check → schema init → sample backfill → Streamlit launch
- `requirements.txt`, `.gitignore`, `.env.example`

## Test results
- `pytest`: **44 passed, 1 skipped** (the skip is the Streamlit-import test; Streamlit is not installed in the build/test env — the app still byte-compiles and is imported when Streamlit is present).
- All modules import with no DATABASE_URL, litellm, or pybaseball present (lazy imports keep the safety/viz helpers unit-testable offline).

## Checker findings applied (diamond-pattern verifiers, fresh context)
- **SQL/security (FAIL fixed):** `apply_row_limit` now *always* wraps queries in an outer `LIMIT max_rows`, so a large or subquery-nested inner `LIMIT` can no longer exceed the hard cap. Removed dead/always-raising session-reset code in `execute_readonly_query` (cleanup is handled by the connection context manager's rollback-then-reset).
- **Ingest (WARNs fixed):** null mask now computed on the cleaned frame so a source date coerced to `NaT` becomes SQL `NULL` instead of slipping into `DATE NOT NULL`; `_to_pg_value` uses an unconditional `pd.isna` guard for NaN/NaT.
- Read-only enforcement, connection lifecycle, batch sizing, numpy coercion, and `game_date` idempotency all verified **PASS**.

## Next steps
- Provide a real `DATABASE_URL` (Supabase) + LLM key in `.env`, then `./run.sh` to backfill and launch.
- Optional: skip empty offseason date chunks during multi-year backfills (minor efficiency).

---

## Checkpoint 2 — 5-year backfill tooling (2021–2025)

### Files created
- `scripts/init_supabase.sql` — schema + indexes on (game_date, game_year, player_name,
  pitch_type, events, launch_speed_angle) incl. a partial barrel index; `ingest_checkpoints`
  table; materialized views `mv_pitcher_arsenals` and `mv_batter_barrels` (+ their indexes).
- `scripts/backfill_5_years.py` — resumable 5-day-chunk backfill for 2021–2025 via
  `execute_values` (batch 2000). Runs the init SQL first, records per-chunk checkpoints
  (skip-if-done), refreshes both materialized views, and runs `VACUUM ANALYZE` on completion.
  numpy→PG coercion + NaT-safe date handling; connection in try/finally; TLS enforced.
- `tests/test_backfill_script.py` — helper + SQL-contract tests.

### Test results
- `pytest`: **56 passed, 1 skipped**. Scripts byte-compile; numpy/NaN/NaT coercion verified.

### Design note — `mv_batter_barrels`
Raw Statcast `player_name` is the **pitcher**; batters are keyed by the `batter` MLBAM id.
The batter-barrel view therefore aggregates by `batter` + `game_year` (barrels =
`launch_speed_angle = 6`, batted balls = `type = 'X'`), with barrel% and average EV/LA.

### EXECUTION STATUS — not run from this environment (blocked, not skipped)
The schema init and the ~3.5M-row backfill were **written and validated but NOT executed here**:
- `list_projects` shows **no Supabase project** on the account, and there is no `DATABASE_URL`
  credential available for a direct `psycopg2` connection.
- `pybaseball` is not installed and the job is a 20–35 min, large outbound-data operation.

To run it (from your machine or any host with the `.env` credentials):
```bash
pip install -r requirements.txt
python scripts/backfill_5_years.py                 # full 2021–2025, resumable
# or a subset:
python scripts/backfill_5_years.py --years 2024 2025
```
Because of `ingest_checkpoints`, the job can be stopped and restarted with no duplicate downloads.
