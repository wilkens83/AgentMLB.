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
