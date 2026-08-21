# Audit: `src/ingest.py`

**Auditor:** Independent ingestion & performance checker
**Date:** 2026-08-21
**Scope:** `src/ingest.py` (with `src/db.py` read for context)

## Summary: PASS (2 low-severity WARNs, no FAILs)

The module is well-structured and correct on every headline requirement:
batching at 2000 rows/page, NaN/NaT → NULL conversion, numpy scalar coercion,
`game_date`-keyed idempotency, per-chunk error isolation, transaction rollback,
and pool return. No data-loss or double-load bug was found. Two low-severity
robustness/efficiency notes are recorded below; neither blocks correct operation.

| # | Audit item | Verdict |
|---|------------|---------|
| 1 | Batch sizing / timeout avoidance | PASS |
| 2 | Null handling (NaN/NaT → SQL NULL) | PASS (WARN: NaT edge case) |
| 3 | numpy type coercion (int64/float64/bool_) | PASS |
| 4 | Duplicate-prevention / idempotency on `game_date` | PASS |
| 5 | Error handling / rollback / pool return | PASS |
| 6 | Off-by-one in date-chunk loop | PASS (WARN: offseason over-fetch) |

---

## 1. Batch sizing for bulk inserts — PASS

- **Line 46:** `BATCH_SIZE = 2000`.
- **Lines 127-129:** `psycopg2.extras.execute_values(cur, insert_sql, rows, page_size=BATCH_SIZE)`.

`execute_values` sends one multi-row `INSERT ... VALUES %s` statement per page of
2000 rows, exactly as specified. This keeps each round-trip bounded and avoids the
single-giant-statement pathology that triggers HTTP/statement timeouts on large
season loads. The DELETE + all INSERT pages run inside one transaction and commit
once (line 130), which is the correct trade-off for idempotency.

Note: the writer connection does not set a `statement_timeout` (unlike the
read-only path in `db.py` line 228). That is appropriate here — you do not want a
bulk load killed mid-flight — and the per-page batching already bounds statement
size, so this is not a defect.

## 2. Null handling (NaN/NaT → SQL NULL) — PASS (WARN on one edge case)

Two independent layers convert missing values to `None`:

- **Line 61:** `clean = clean.astype(object).where(pd.notnull(df[available]), None)`
  replaces NaN/NaT with `None` across the frame.
- **Lines 76-79:** `_to_pg_value` additionally maps `float` NaN to `None`.

This correctly yields SQL NULL for the normal case (value already missing in the
source pull).

**WARN (low severity) — lines 61 + 76-79.** The mask on line 61 is computed from
the *original* `df[available]`, not from `clean`. `game_date` is transformed to a
`date` on line 59 *before* the mask is applied. If `pd.to_datetime(..., errors="coerce")`
turns a *non-null but invalid* `game_date` string into `NaT`, the original value is
non-null so the mask keeps the `NaT` rather than nulling it. `_to_pg_value` will not
rescue it either: its NaN guard on line 76 only fires for `isinstance(item, float)`,
and `pd.NaT` is not a `float`, so the `pd.isna` check is skipped and `NaT` is passed
to psycopg2 for a `DATE NOT NULL` column → adaptation/insert error for that batch.
This only bites on genuinely malformed source dates (rare), but it is a real gap.

*Fix (either is sufficient):*
- Compute the mask from the cleaned frame: `.where(pd.notnull(clean), None)`; or
- Broaden the null guard in `_to_pg_value` to catch `NaT`/`datetime64` NaT, e.g.
  ```python
  try:
      if item is None or pd.isna(item):
          return None
  except (TypeError, ValueError):
      pass
  ```
  (drop the `isinstance(item, float)` narrowing — `pd.isna` is safe on these scalars).

## 3. numpy type coercion (int64/float64/bool_) — PASS

- **Lines 73-74:** `item = value.item() if hasattr(value, "item") else value`.

`numpy.int64`, `numpy.float64`, and `numpy.bool_` all expose `.item()`, which
returns a native Python `int`/`float`/`bool`. After `df.to_numpy()` (line 85) the
frame collapses to an object array whose numeric cells remain boxed numpy scalars,
so each is unboxed to a psycopg2-adaptable Python scalar before insertion. Native
Python scalars (which lack `.item()`) fall through unchanged. This is the correct
and complete handling of the "numpy scalars are not natively adaptable" hazard.

## 4. Duplicate-prevention / idempotency on `game_date` — PASS

- **Lines 115, 122-126:** distinct `game_date`s in the batch are collected
  (`_game_dates`, lines 88-93) and, when `replace_existing=True`, existing rows for
  those dates are deleted with `DELETE FROM statcast_pitches WHERE game_date = ANY(%s)`
  *inside the same transaction* as the insert (single commit on line 130).

Re-running a backfill or the daily update for the same day(s) therefore deletes the
prior copy and reloads, giving true idempotency keyed on `game_date`. Because DELETE
and INSERT share one transaction, a crash mid-load cannot leave the table with the
old rows removed and the new rows missing. `run_daily_update` (lines 205-215) adds a
belt-and-suspenders `date_has_data` pre-check, and still uses `replace_existing=True`,
so a partially-loaded day is fully refreshed rather than appended to. Correct.

## 5. Error handling / rollback / pool return — PASS

- **Per-chunk isolation (lines 168-174):** each chunk's fetch+insert is wrapped in
  `try/except Exception`; failures are printed and the loop advances (`current = chunk_end + 1`).
  One bad range cannot abort a multi-season backfill.
- **Transaction rollback (lines 131-133):** `insert_dataframe` rolls back and
  re-raises on any error, so a failed batch never commits a partial DELETE.
- **Connection return (db.py lines 115-133):** `get_db_connection` is a
  `contextmanager` whose `finally` block rolls back, resets session flags, and calls
  `pool.putconn(conn)` unconditionally — the connection is returned to the pool even
  when the body raises. `date_has_data` (lines 184-194) and `init_supabase_schema`
  follow the same pattern.

All three sub-requirements are satisfied.

## 6. Off-by-one in the date-chunk loop — PASS (WARN on offseason over-fetch)

- **Lines 164-174:**
  ```python
  current = start_date
  while current <= end_date:
      chunk_end = min(current + datetime.timedelta(days=chunk_days), end_date)
      ... fetch(current, chunk_end) ...   # pybaseball statcast() range is INCLUSIVE
      current = chunk_end + datetime.timedelta(days=1)
  ```

No day is skipped and no day is loaded twice: each iteration covers the inclusive
range `[current, chunk_end]`, and the next iteration starts at `chunk_end + 1`, so
ranges are contiguous and non-overlapping. The final chunk clamps to `end_date` via
`min(...)`, and the loop terminates cleanly (`current` becomes `end_date + 1`). The
per-day idempotent DELETE (item 4) would also neutralize any accidental overlap, but
none exists here. **Correct — no off-by-one.**

Minor note: with `chunk_days=5` each inclusive chunk actually spans 6 calendar days
(`current` through `current+5`). This is a cosmetic naming nuance, not a bug — days
are still covered exactly once.

**WARN (low severity) — lines 160-161.** `start_date` is fixed at `Mar 20` of
`start_year` and `end_date` at `Nov 5` of `end_year`. For a multi-year backfill the
loop marches straight through each winter offseason (e.g. `2022-11-06 .. 2023-03-19`),
issuing pybaseball requests that return empty frames. This is wasteful, not
incorrect (`insert_dataframe` returns 0 on empty input, line 104-105; empty chunks
are guarded on line 170). *Optional fix:* restart `current` at `Mar 20` of the next
season once `chunk_end` passes that year's `Nov 5`, to skip the dead months.

---

## Additional observations (informational, no action required)

- **Column/value alignment (lines 113-117):** `columns` and each row tuple both
  derive from the same `clean` column order (`available`), so positional INSERT
  alignment is guaranteed even under schema drift.
- **Schema drift tolerance (line 56):** only columns present in *both* the pull and
  `COLS` are persisted, so a Statcast schema change adds/removes columns gracefully
  rather than crashing the load.
- **`ANY(%s)` array adaptation (lines 123-126):** passing a Python `list` of `date`
  objects to `ANY(%s)` is adapted by psycopg2 to a PostgreSQL array; correct usage.
