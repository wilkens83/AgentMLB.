#!/usr/bin/env bash
#
# MLB Analytics System launcher.
#   1. Verifies the environment (.env / DATABASE_URL).
#   2. Installs dependencies if needed.
#   3. Initializes the Supabase schema (idempotent).
#   4. Backfills a sample season (skippable).
#   5. Launches the Streamlit dashboard.
#
# Usage:
#   ./run.sh                       # backfill 2024 then launch
#   SKIP_BACKFILL=1 ./run.sh       # just launch the UI
#   SAMPLE_START=2023 SAMPLE_END=2024 ./run.sh
#
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ] && [ -z "${DATABASE_URL:-}" ]; then
  echo "ERROR: No .env file and DATABASE_URL is not set." >&2
  echo "Copy .env.example to .env and fill in your Supabase DATABASE_URL." >&2
  exit 1
fi

# Install dependencies if Streamlit is not importable.
if ! python3 -c "import streamlit" >/dev/null 2>&1; then
  echo "Installing dependencies from requirements.txt..."
  pip install -r requirements.txt
fi

# Initialize the schema (safe to run repeatedly).
echo "Initializing Supabase schema..."
python3 -c "import sys; sys.path.insert(0, 'src'); from db import init_supabase_schema; init_supabase_schema()"

# Optional sample backfill.
if [ "${SKIP_BACKFILL:-0}" != "1" ]; then
  START="${SAMPLE_START:-2024}"
  END="${SAMPLE_END:-2024}"
  echo "Backfilling sample Statcast data for ${START}..${END} (this can take a while)."
  python3 -c "import sys; sys.path.insert(0, 'src'); from ingest import backfill_statcast; backfill_statcast(${START}, ${END})" \
    || echo "Backfill encountered errors; continuing to launch the UI."
fi

echo "Launching Streamlit..."
exec streamlit run src/app.py
