Work in a loop until the SQL query passes all checks. Do not stop early.

GOAL:
Generate and validate a PostgreSQL (Supabase) SQL query for: "[USER_NATURAL_LANGUAGE_QUERY]"

CRITERIA (All must pass):
1. Query executes without syntax errors on PostgreSQL.
2. Execution time is under 1.5 seconds (statement_timeout enforced server-side).
3. Returns at least 1 row (unless the true answer is zero) and never returns NULL for primary metrics.
4. Uses case-insensitive matching (`ILIKE`) for player names.
5. Adheres to read-only constraints (`SELECT`/`WITH` only) and includes a `LIMIT`.

EACH PASS:
1. DRAFT: Generate the SQL query grounded in the `statcast_pitches` schema.
2. EXECUTE: Run the query via `db.execute_readonly_query` against Supabase.
3. SCORE: Rate the result (1–10) against each criterion. Be strict.
4. GAPS: If an error occurs (e.g., column mismatch, type error), identify the root cause.
5. CALL:
   - If every criterion scores 8+, return the final JSON payload and write "DONE".
   - If any score is below 8, write "NEXT PASS" and fix the single weakest gap.
   - Max 4 passes. If the limit is reached, return a graceful fallback message.
