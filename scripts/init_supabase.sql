-- scripts/init_supabase.sql
-- Initializes the MLB Statcast analytics schema on Supabase / PostgreSQL.
-- Safe to run repeatedly: every object uses IF NOT EXISTS.

-- 1. Main Statcast Pitches Table
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
    pitch_number INT,
    inning INT,
    outs_when_up INT,
    inning_topbot VARCHAR(10)
);

-- 2. Performance Indexes for Fast Text-to-SQL Execution
CREATE INDEX IF NOT EXISTS idx_statcast_date ON statcast_pitches(game_date);
CREATE INDEX IF NOT EXISTS idx_statcast_player ON statcast_pitches(player_name);
CREATE INDEX IF NOT EXISTS idx_statcast_pitch_type ON statcast_pitches(pitch_type);
CREATE INDEX IF NOT EXISTS idx_statcast_year ON statcast_pitches(game_year);
CREATE INDEX IF NOT EXISTS idx_statcast_events ON statcast_pitches(events);
-- General index on contact quality, plus a partial index tuned for barrels (=6).
CREATE INDEX IF NOT EXISTS idx_statcast_launch_speed_angle ON statcast_pitches(launch_speed_angle);
CREATE INDEX IF NOT EXISTS idx_statcast_barrels ON statcast_pitches(launch_speed_angle) WHERE launch_speed_angle = 6;

-- 3. Checkpoint Tracking Table (prevents redundant downloads; makes backfill resumable)
CREATE TABLE IF NOT EXISTS ingest_checkpoints (
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    pitch_count INT NOT NULL,
    ingested_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (start_date, end_date)
);

-- 4a. Materialized View: Pitcher Arsenal Summary (pre-aggregated for instant UI lookups)
--     NOTE: In raw Statcast, player_name is the PITCHER's name.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_pitcher_arsenals AS
SELECT
    player_name,
    game_year,
    pitch_name,
    pitch_type,
    COUNT(*) AS total_pitches,
    ROUND(AVG(release_speed)::NUMERIC, 1) AS avg_velocity,
    ROUND(AVG(release_spin_rate)::NUMERIC, 0) AS avg_spin_rate,
    ROUND(AVG(CASE WHEN description IN ('swinging_strike', 'swinging_strike_blocked')
                   THEN 1.0 ELSE 0.0 END)::NUMERIC * 100, 1) AS whiff_percent
FROM statcast_pitches
WHERE pitch_name IS NOT NULL
GROUP BY player_name, game_year, pitch_name, pitch_type;

CREATE INDEX IF NOT EXISTS idx_mv_arsenal ON mv_pitcher_arsenals(player_name, game_year);

-- 4b. Materialized View: Batter Barrel Summary
--     Batters are identified by their MLBAM id (`batter`); raw Statcast does not
--     carry the batter's name (player_name is the pitcher). A barrel is
--     launch_speed_angle = 6; a batted ball into play is type = 'X'.
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_batter_barrels AS
SELECT
    batter,
    game_year,
    COUNT(*) FILTER (WHERE type = 'X') AS batted_balls,
    COUNT(*) FILTER (WHERE launch_speed_angle = 6) AS barrels,
    ROUND(
        (COUNT(*) FILTER (WHERE launch_speed_angle = 6))::NUMERIC
        / NULLIF(COUNT(*) FILTER (WHERE type = 'X'), 0) * 100, 1
    ) AS barrel_percent,
    ROUND(AVG(launch_speed) FILTER (WHERE launch_speed_angle = 6)::NUMERIC, 1) AS avg_barrel_ev,
    ROUND(AVG(launch_angle) FILTER (WHERE launch_speed_angle = 6)::NUMERIC, 1) AS avg_barrel_la,
    ROUND(AVG(launch_speed) FILTER (WHERE type = 'X')::NUMERIC, 1) AS avg_exit_velocity
FROM statcast_pitches
WHERE batter IS NOT NULL
GROUP BY batter, game_year;

CREATE INDEX IF NOT EXISTS idx_mv_barrels ON mv_batter_barrels(batter, game_year);
