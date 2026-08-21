-- scripts/002_enable_rls.sql
-- Enable Row Level Security with public read-only (anon/authenticated) access
-- and full access restricted to service_role. Applied to Supabase project
-- agentmlb-statcast as versioned migration `enable_rls_policies`.
--
-- Idempotent: policies are dropped-if-exists before creation so this can be
-- re-applied safely. The direct `postgres` role (used by the psycopg2 backfill)
-- owns these tables and bypasses RLS, so ingestion is unaffected.

-- 1. Enable RLS on both tables
ALTER TABLE statcast_pitches ENABLE ROW LEVEL SECURITY;
ALTER TABLE ingest_checkpoints ENABLE ROW LEVEL SECURITY;

-- 2. Public/anon read-only access for analytics querying
DROP POLICY IF EXISTS "Allow public read-only access to statcast pitches" ON statcast_pitches;
CREATE POLICY "Allow public read-only access to statcast pitches"
ON statcast_pitches
FOR SELECT
TO anon, authenticated
USING (true);

-- 3. Restrict all modifications on statcast_pitches to service_role
DROP POLICY IF EXISTS "Allow service role full access to statcast pitches" ON statcast_pitches;
CREATE POLICY "Allow service role full access to statcast pitches"
ON statcast_pitches
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);

-- 4. Checkpoints table: only service_role can view or modify
DROP POLICY IF EXISTS "Allow service role full access to checkpoints" ON ingest_checkpoints;
CREATE POLICY "Allow service role full access to checkpoints"
ON ingest_checkpoints
FOR ALL
TO service_role
USING (true)
WITH CHECK (true);
