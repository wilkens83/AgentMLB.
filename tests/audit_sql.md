# SQL Safety & Security Audit — `src/db.py`, `src/agent.py`

**Auditor:** Independent security & SQL checker
**Date:** 2026-08-21
**Scope:** SQL injection / read-only enforcement, row cap + statement timeout, connection lifecycle, correctness.

---

## Summary Verdict: **FAIL** (one material row-cap gap; core design otherwise sound)

The core defense-in-depth architecture is solid: LLM SQL is validated by a regex allowlist **and** executed inside a genuine PostgreSQL `READ ONLY` transaction with a server-side `statement_timeout`, and connections are always returned to the pool with the read-only flag reset. Writes/DDL are blocked at the database level even if the regex is bypassed.

The blocking issue is that the **hard row cap is NOT applied to every executed query**: any query that already contains a `LIMIT` (including a huge one, or a `LIMIT` buried in a subquery) skips the outer cap entirely, so `MAX_ROWS` is not enforced. This is intentional per `test_apply_row_limit_respects_existing_limit`, but it defeats the stated requirement.

| # | Audit item | Verdict |
|---|-----------|---------|
| 1 | SQL injection paths / read-only truly enforced (SELECT/WITH only, DDL/DML & multi-statement rejected) | **PASS** (DB-level `READ ONLY` is the real guarantee; regex is defense-in-depth) |
| 2a | Hard row cap (LIMIT) applied to **every** executed query | **FAIL** (bypassed whenever any `LIMIT` is present) |
| 2b | Server-side `statement_timeout` on every query | **PASS** |
| 3 | Connection lifecycle: always returned to pool, no readonly-flag leakage, rollback on error | **PASS** |
| 4 | Correctness bugs (crash / silent mutation) | **WARN** (non-functional reset in `finally`; over/under-broad regex) |

---

## Item 1 — SQL injection & read-only enforcement — **PASS**

**`assert_readonly_sql`, `db.py:59-76`**

- Empty/blank rejected (`db.py:66-67`). PASS.
- Multi-statement rejected: after `rstrip(";")`, any remaining `;` raises (`db.py:69-71`). Correctly rejects `SELECT 1; DROP TABLE ...`. PASS.
- Prefix allowlist requires the statement to start with `select`/`with` (`db.py:50, 72-73`). PASS.
- Forbidden-keyword denylist blocks `insert/update/delete/drop/alter/create/truncate/grant/revoke/...` with `\b` word boundaries (`db.py:51-55, 74-75`), so `created_at`-style identifiers are not tripped (verified by existing test). PASS.

**Real enforcement — `execute_readonly_query`, `db.py:224-240`**

The authoritative guarantee is **not** the regex but `conn.set_session(readonly=True, autocommit=False)` (`db.py:226`): PostgreSQL rejects `INSERT/UPDATE/DELETE/DDL`, `nextval()`, `SELECT ... INTO`, etc. inside a `READ ONLY` transaction. So even a regex bypass cannot mutate data. PASS.

**No parameter injection risk in the executed statement.** The whole validated statement is executed as one string (`db.py:229`); there is no user-controlled string concatenation into a different SQL context, and the only parameterized call (`SET LOCAL statement_timeout = %s`) is bound, not formatted (`db.py:228`). PASS.

**Minor notes (not failures):**
- **Denylist over-blocks string literals** (`db.py:51-55`): a legitimate query such as `... WHERE x = 'set'` or `'created'` is rejected because the regex scans literal text too. Conservative (fails closed) — WARN only.
- **Denylist does not list `into`** (`db.py:51-55`): `SELECT ... INTO newtable` passes the regex, but is still blocked by the `READ ONLY` transaction at the DB. Consider adding `into` for cleaner defense-in-depth.
- **Semicolon inside a string literal** (`db.py:70`) is treated as multi-statement and rejected — a false positive, but safe.

---

## Item 2a — Hard row cap on every query — **FAIL**

**`apply_row_limit`, `db.py:79-86` (specifically the early return at `db.py:84-85`)**

```python
if _LIMIT_RE.search(sql):   # db.py:84  -> _LIMIT_RE = \blimit\b\s+\d+  (db.py:56)
    return sql              # db.py:85  -> cap SKIPPED entirely
return f"SELECT * FROM (\n{sql}\n) AS _capped LIMIT {int(max_rows)}"  # db.py:86
```

Problems:

1. **A `LIMIT` larger than `MAX_ROWS` is honored verbatim.** `SELECT * FROM statcast_pitches LIMIT 5000000` matches `_LIMIT_RE`, so the cap is skipped and the query returns up to 5,000,000 rows despite `MAX_ROWS = 1000`. The cap is advisory, not hard.
2. **A `LIMIT` in a subquery disables the outer cap.** `SELECT * FROM statcast_pitches WHERE pitcher IN (SELECT pitcher FROM statcast_pitches LIMIT 5)` matches `_LIMIT_RE`, so the *outer* result set (potentially the whole table) is returned uncapped.

The 5s `statement_timeout` and the `READ ONLY` transaction bound the blast radius (no mutation; query is time-limited), but memory use from `cur.fetchall()` (`db.py:231`) into a DataFrame is still unbounded within that window. The requirement "hard row cap applied to every executed query" is not met.

**Fix (concrete):** always wrap; let the outer `LIMIT` clamp any inner one. Remove the early return:

```python
def apply_row_limit(sql: str, max_rows: int = MAX_ROWS) -> str:
    # Always cap the final result set. An inner LIMIT smaller than max_rows
    # still wins; a larger or subquery LIMIT is clamped by the outer LIMIT.
    return f"SELECT * FROM (\n{sql}\n) AS _capped LIMIT {int(max_rows)}"
```

Outer wrapping preserves inner `ORDER BY ... LIMIT` semantics (top-N is computed inside; the outer only caps the count). Note this changes the behavior asserted by `test_apply_row_limit_respects_existing_limit` (`tests/test_sql_agent.py:65-67`), which should be updated to assert clamping instead of pass-through.

---

## Item 2b — Server-side statement timeout — **PASS**

**`db.py:228`** — `cur.execute("SET LOCAL statement_timeout = %s", (int(STATEMENT_TIMEOUT_MS),))`

- Runs with `autocommit=False`, so the first `execute` opens a transaction and `SET LOCAL` is scoped to it; the capped query (`db.py:229`) runs in that same transaction before any commit/rollback, so the timeout applies. PASS.
- Bare integer is interpreted by PostgreSQL as milliseconds (`5000` = 5s). Correct. PASS.
- Value is parameter-bound, not string-formatted. PASS.

---

## Item 3 — Connection lifecycle — **PASS**

**`get_db_connection`, `db.py:115-133`**

- `getconn` then `try/finally: putconn` guarantees the connection is always returned, including on exceptions in the body (`db.py:124-133`). If `getconn()` itself raises, no leak (nothing to return). PASS.
- **No readonly-flag leakage across pooled connections.** The `finally` rolls back **first** (`db.py:129`) and only then calls `conn.set_session(readonly=False, autocommit=False)` (`db.py:130`). The rollback-before-reset ordering is important and correct: `set_session` cannot run inside an open transaction, so rolling back first lets the reset succeed, guaranteeing the next borrower does not inherit `READ ONLY`. PASS.
- **Rollback on error.** Both `init_supabase_schema` (`db.py:200-208`) and `execute_readonly_query` (`db.py:233-235`) roll back in `except` and re-raise. PASS.

**Minor (robustness) WARN:** if the connection is already dead, the `finally` cleanup is swallowed (`db.py:131-132`) and the broken connection is still returned via `putconn` (`db.py:133`), where it may be handed out again. Consider `pool.putconn(conn, close=True)` when cleanup raises, so poisoned connections are discarded. Not a correctness failure under normal operation.

---

## Item 4 — Correctness bugs — **WARN**

**4.1 Non-functional `finally` reset in `execute_readonly_query` (`db.py:236-240`).**
After a successful query the transaction is still open (no commit), so `conn.set_session(readonly=False, autocommit=False)` at `db.py:238` raises `psycopg2.ProgrammingError: set_session cannot be used inside a transaction`, which is silently swallowed by the `except` at `db.py:239-240`. The reset here is therefore **dead code** — it never actually clears the flag. This is harmless only because `get_db_connection`'s `finally` (`db.py:128-130`) rolls back first and then resets, which is what truly clears the flag. Recommend either removing the ineffective block at `db.py:236-240` (rely on the context manager) or adding `conn.rollback()` before the `set_session` call there. No data-integrity impact.

**4.2 No silent-mutation path found.** Every execution route either runs under a `READ ONLY` transaction (`db.py:226`) or is the explicit, committed DDL init path (`db.py:198-208`); the LLM/agent path (`agent.py:196-217` → `execute_readonly_query`) can only reach the read-only executor. PASS on "no silent mutation."

**4.3 `agent.py` — no crash paths of note.** `parse_agent_json` (`agent.py:65-86`) tolerates fences and embedded objects and raises `ValueError` cleanly; `query_mlb_agent` (`agent.py:196-217`) wraps the whole pipeline in `try/except` and surfaces errors as `result["error"]` with an empty DataFrame, so a bad LLM response or a rejected query cannot crash the caller. `assert_readonly_sql` is re-applied in `generate_sql` (`agent.py:187`) before execution — good redundancy. PASS.

---

## Recommended actions (priority order)

1. **(FAIL) Fix the row cap** — `db.py:84-85`: always wrap in an outer `LIMIT max_rows`; update `tests/test_sql_agent.py:65-67` to assert clamping.
2. **(WARN) Remove/repair the dead reset** at `db.py:236-240` (or `rollback()` before it).
3. **(WARN) Discard poisoned connections** — `db.py:133`: `close=True` when cleanup fails.
4. **(nit) Defense-in-depth** — add `into` to `_FORBIDDEN_RE` (`db.py:51-55`); optionally narrow the denylist so it does not scan string literals.

_No source files were modified by this audit._
