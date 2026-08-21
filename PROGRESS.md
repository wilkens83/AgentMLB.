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

---

## Checkpoint 3 — Supabase provisioned via MCP + RLS

### Database (created & verified through the Supabase MCP)
- Project **agentmlb-statcast** (ref `ebtumpzsfutemxjnnuni`, us-east-1, free tier), status ACTIVE_HEALTHY.
- Migration `init_statcast_schema`: `statcast_pitches`, `ingest_checkpoints`, 7 indexes
  (+2 PKs), materialized views `mv_pitcher_arsenals` + `mv_batter_barrels`. Verified via list_tables/pg_matviews.
- Migration `enable_rls_policies` (`scripts/002_enable_rls.sql`): RLS ON both tables;
  `statcast_pitches` → anon/authenticated SELECT, service_role ALL; `ingest_checkpoints` → service_role ALL.
  Verified via pg_policy.
- API URL: https://ebtumpzsfutemxjnnuni.supabase.co

### Backfill — HANDOFF (cannot run from this remote container)
Verified empirically: raw Postgres TCP is blocked here (`:5432` and pooler `:6543` time out; egress is
HTTPS-only), so the psycopg2 backfill cannot connect. Baseball Savant IS reachable over HTTPS (200).
Run the backfill from a host with normal Postgres egress:
```bash
# .env must contain the direct DATABASE_URL (Dashboard -> Settings -> Database -> Connection string / URI)
pip install -r requirements.txt
python scripts/backfill_5_years.py            # 2021-2025, resumable via ingest_checkpoints
```
The script re-applies the (idempotent) schema, loads in 5-day chunks, refreshes both MVs, and VACUUM ANALYZEs.

---

## Checkpoint 4 — End-to-end verification audit

### Phase 1 — Database (verified live via Supabase MCP)
- RLS enabled on `statcast_pitches` + `ingest_checkpoints`; all 3 policies present (anon/authenticated SELECT; service_role ALL; checkpoints service_role ALL).
- 7 indexes + 2 PKs on base tables; both MVs exist and `ispopulated=true`; MV indexes present.
- Data: `statcast_pitches` = 0 rows, `ingest_checkpoints` = 0 rows → backfill not yet run (handoff pending). Row-by-year / null-rate checks N/A until loaded.

### Phase 2 — Backend & agent
- `pytest`: 56 passed, 1 skipped (skip only when Streamlit absent).
- 3 review queries: read-only SELECT ✅, ILIKE where a name is matched ✅, outer LIMIT 1000 cap ✅, valid Plotly JSON ✅. Negative controls (DROP/DELETE/multi-statement) rejected ✅.
- Each candidate SQL validated against the real schema via EXPLAIN — valid plans using idx_statcast_barrels / idx_statcast_pitch_type / idx_statcast_year.
- Note: live LLM generation + query execution need an API key + Postgres egress (neither in this container); the deterministic guard + schema-validity layers were verified instead.

### Phase 3 — Streamlit UI (real headless launch + Playwright/Chromium)
- Launched `streamlit run src/app.py` headless (HTTP 200); captured dashboard shell, live chat feed (graceful st.error when litellm absent), and the full success render path (SQL expander + Plotly scatter + data table + CSV button) via app.render_result with sample data.

### Phase 4 — CI/CD & git (FIX APPLIED)
- Added `.github/workflows/daily_ingest.yml` (was missing): cron `0 8 * * *` = 04:00 EDT, `secrets.DATABASE_URL`, workflow_dispatch, concurrency guard, runs `run_daily_update()`.
- Working tree clean. Branch is `claude/mlb-analysis-system-dntpr7` (the repo's default branch) — this managed session cannot push to a literal `main`; the assigned branch serves as main.

---

## Checkpoint 5 — PitcherStat Matchup & Performance Grille

### Phase 1 — DB (migration 003, applied live via MCP)
- Enriched schema: added `inning`, `outs_when_up`, `inning_topbot` (+ indexes on pitcher/batter/game_pk).
  Synced into db.py CREATE_TABLE_SQL, scripts/init_supabase.sql, and both ingestion COLS lists.
- New objects (verified): `mv_bvp_matchups` (idx pitcher,batter), `mv_pitcher_trends_and_blowup`
  (idx pitcher,game_date), `mv_bullpen_l10` (idx team), view `v_team_platoon_ops`.
- **RA/9 substitution**: Statcast has no earned runs, so ERA is not derivable — all ERA-style
  metrics use RA/9 (runs allowed per 9) from score-state columns; IP derived from recorded outs.
  Team attribution via inning_topbot; starter/reliever via first pitcher of each game.

### Phase 2 — Agent + helpers
- `src/matchup.py`: pure helpers (BVP slash line, IP-from-outs, K/9, BB/9, rate_advantage,
  sample-size fallback, highlight rule) mirroring the SQL.
- Agent SYSTEM_PROMPT: documents the 4 new views, the 5 question categories, RA/9 note, BVP
  AB<5 handedness fallback, and returns "not available" for weather & salary (no fabrication).
- CLAUDE.md: matchup analytics + RA/9 + data-gap notes.

### Phase 3 — Streamlit
- `src/app.py` restructured into tabs: Chat + "⚾ Matchup Duel & BVP Analyzer" (Starter Duel card
  with K/BB advantage badges, BVP grid with OPS>=.900 / AB>=10 highlight, Bullpen L10 + platoon,
  3 sidebar deep-dive presets). Graceful empty/error states when DB is unloaded. Verified via
  real headless render (screenshot 04_matchup_tab).

### Phase 4 — Tests
- `tests/test_matchup_analytics.py` added. `pytest`: **68 passed, 0 skipped**. All modules py_compile.
- Matchup SQL (BVP grid, trends, bullpen+platoon join) validated against the live views via EXPLAIN.
