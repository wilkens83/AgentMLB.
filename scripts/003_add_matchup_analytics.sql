-- scripts/003_add_matchup_analytics.sql
-- PitcherStat Matchup & Performance Grille: schema enrichment + analytics views.
--
-- IMPORTANT DATA NOTE (honest substitutions):
--   * Statcast pitch data has NO earned-run flag, so a true ERA is impossible.
--     Everywhere ERA is requested we compute RA/9 (Runs Allowed per 9), derived
--     from the score-state columns (post_bat_score - bat_score). Columns are
--     named ra9 and labeled as such.
--   * Innings Pitched (IP) is derived from outs recorded via `events`, using the
--     standard out-mapping below. Doubleheaders are separated by game_pk.
--   * Team attribution uses inning_topbot: 'Top' => away team bats / home team
--     pitches; 'Bot' => home team bats / away team pitches.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS + CREATE ... IF NOT EXISTS. MVs are
-- dropped-if-exists so re-applying picks up definition changes.

-- ---------------------------------------------------------------------------
-- 1. Schema enrichment (columns pybaseball already returns but we didn't store)
-- ---------------------------------------------------------------------------
ALTER TABLE statcast_pitches ADD COLUMN IF NOT EXISTS inning INT;
ALTER TABLE statcast_pitches ADD COLUMN IF NOT EXISTS outs_when_up INT;
ALTER TABLE statcast_pitches ADD COLUMN IF NOT EXISTS inning_topbot VARCHAR(10);

-- Supporting indexes for matchup lookups.
CREATE INDEX IF NOT EXISTS idx_statcast_pitcher ON statcast_pitches(pitcher);
CREATE INDEX IF NOT EXISTS idx_statcast_batter ON statcast_pitches(batter);
CREATE INDEX IF NOT EXISTS idx_statcast_game_pk ON statcast_pitches(game_pk);

-- ---------------------------------------------------------------------------
-- 2. mv_bvp_matchups — Batter vs. Pitcher head-to-head (AB/H/HR/AVG/OBP/SLG/OPS)
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS mv_bvp_matchups CASCADE;
CREATE MATERIALIZED VIEW mv_bvp_matchups AS
WITH pa AS (
    -- One row per completed plate appearance (events only set on the final pitch).
    SELECT batter, pitcher, player_name, p_throws, events
    FROM statcast_pitches
    WHERE events IS NOT NULL AND events <> ''
),
agg AS (
    SELECT
        batter,
        pitcher,
        max(player_name) AS pitcher_name,
        max(p_throws)    AS p_throws,
        count(*) FILTER (WHERE events IN ('single','double','triple','home_run')) AS h,
        count(*) FILTER (WHERE events = 'home_run') AS hr,
        count(*) FILTER (WHERE events = 'walk') AS bb,
        count(*) FILTER (WHERE events = 'hit_by_pitch') AS hbp,
        count(*) FILTER (WHERE events IN ('sac_fly','sac_fly_double_play')) AS sf,
        count(*) FILTER (WHERE events NOT IN
            ('walk','hit_by_pitch','sac_fly','sac_bunt','catcher_interf',
             'sac_fly_double_play','sac_bunt_double_play')) AS ab,
        (count(*) FILTER (WHERE events = 'single')
         + 2 * count(*) FILTER (WHERE events = 'double')
         + 3 * count(*) FILTER (WHERE events = 'triple')
         + 4 * count(*) FILTER (WHERE events = 'home_run')) AS tb
    FROM pa
    GROUP BY batter, pitcher
)
SELECT
    batter, pitcher, pitcher_name, p_throws,
    ab, h, hr, bb, hbp, sf, tb,
    round(h::numeric / NULLIF(ab, 0), 3) AS avg,
    round((h + bb + hbp)::numeric / NULLIF(ab + bb + hbp + sf, 0), 3) AS obp,
    round(tb::numeric / NULLIF(ab, 0), 3) AS slg,
    round((h + bb + hbp)::numeric / NULLIF(ab + bb + hbp + sf, 0)
          + tb::numeric / NULLIF(ab, 0), 3) AS ops
FROM agg;

CREATE INDEX IF NOT EXISTS idx_mv_bvp ON mv_bvp_matchups(pitcher, batter);

-- ---------------------------------------------------------------------------
-- 3. mv_pitcher_trends_and_blowup — Starter momentum & volatility (RA-based)
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS mv_pitcher_trends_and_blowup CASCADE;
CREATE MATERIALIZED VIEW mv_pitcher_trends_and_blowup AS
WITH gp AS (
    SELECT
        pitcher, player_name, game_pk, game_date, game_year,
        CASE WHEN inning_topbot = 'Top' THEN home_team ELSE away_team END AS pitcher_team,
        inning, at_bat_number, pitch_number, events,
        (post_bat_score - bat_score) AS runs_on_play,
        CASE events
            WHEN 'strikeout' THEN 1 WHEN 'field_out' THEN 1 WHEN 'force_out' THEN 1
            WHEN 'sac_fly' THEN 1 WHEN 'sac_bunt' THEN 1 WHEN 'fielders_choice_out' THEN 1
            WHEN 'fielders_choice' THEN 1 WHEN 'other_out' THEN 1
            WHEN 'strikeout_double_play' THEN 2 WHEN 'grounded_into_double_play' THEN 2
            WHEN 'double_play' THEN 2 WHEN 'sac_fly_double_play' THEN 2
            WHEN 'sac_bunt_double_play' THEN 2 WHEN 'triple_play' THEN 3
            ELSE 0
        END AS outs
    FROM statcast_pitches
    WHERE inning_topbot IS NOT NULL
),
first_pitcher AS (
    SELECT game_pk, pitcher_team, pitcher,
        ROW_NUMBER() OVER (PARTITION BY game_pk, pitcher_team
                           ORDER BY inning, at_bat_number, pitch_number) AS rn
    FROM gp
),
starters AS (SELECT DISTINCT game_pk, pitcher_team, pitcher FROM first_pitcher WHERE rn = 1),
per_start AS (
    SELECT
        g.pitcher,
        max(g.player_name) AS pitcher_name,
        g.game_pk, g.game_date, g.game_year,
        round(sum(g.outs) / 3.0, 1) AS ip,
        sum(g.runs_on_play) AS runs_allowed
    FROM gp g
    JOIN starters s
      ON s.game_pk = g.game_pk AND s.pitcher = g.pitcher AND s.pitcher_team = g.pitcher_team
    GROUP BY g.pitcher, g.game_pk, g.game_date, g.game_year
)
SELECT
    pitcher, pitcher_name, game_pk, game_date, game_year, ip, runs_allowed,
    -- Rolling RA/9 over the last 3 and 5 starts.
    round(sum(runs_allowed) OVER w3 * 9.0 / NULLIF(sum(ip) OVER w3, 0), 2) AS ra9_roll3,
    round(sum(runs_allowed) OVER w5 * 9.0 / NULLIF(sum(ip) OVER w5, 0), 2) AS ra9_roll5,
    CASE
        WHEN sum(runs_allowed) OVER w3 * 9.0 / NULLIF(sum(ip) OVER w3, 0)
             < sum(runs_allowed) OVER w5 * 9.0 / NULLIF(sum(ip) OVER w5, 0) THEN 'down'
        WHEN sum(runs_allowed) OVER w3 * 9.0 / NULLIF(sum(ip) OVER w3, 0)
             > sum(runs_allowed) OVER w5 * 9.0 / NULLIF(sum(ip) OVER w5, 0) THEN 'up'
        ELSE 'flat'
    END AS ra_trend,   -- 'down' = improving (fewer runs), 'up' = worsening
    (runs_allowed >= 4 OR ip < 4.0) AS is_blowup,
    -- Season-level context repeated per start row.
    round(avg(ip) OVER season, 1) AS avg_ip_per_start,
    round(avg(CASE WHEN (runs_allowed >= 4 OR ip < 4.0) THEN 1.0 ELSE 0.0 END) OVER season * 100, 1) AS blowup_pct,
    round(sum(runs_allowed) OVER season * 9.0 / NULLIF(sum(ip) OVER season, 0), 2) AS season_ra9
FROM per_start
WINDOW
    w3 AS (PARTITION BY pitcher ORDER BY game_date ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
    w5 AS (PARTITION BY pitcher ORDER BY game_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
    season AS (PARTITION BY pitcher, game_year);

CREATE INDEX IF NOT EXISTS idx_mv_trends ON mv_pitcher_trends_and_blowup(pitcher, game_date);

-- ---------------------------------------------------------------------------
-- 4. mv_bullpen_l10 — Bullpen fatigue & recent form over each team's last 10 games
-- ---------------------------------------------------------------------------
DROP MATERIALIZED VIEW IF EXISTS mv_bullpen_l10 CASCADE;
CREATE MATERIALIZED VIEW mv_bullpen_l10 AS
WITH gp AS (
    SELECT
        pitcher, game_pk, game_date, events, description,
        CASE WHEN inning_topbot = 'Top' THEN home_team ELSE away_team END AS pitcher_team,
        inning, at_bat_number, pitch_number,
        (post_bat_score - bat_score) AS runs_on_play,
        CASE events
            WHEN 'strikeout' THEN 1 WHEN 'field_out' THEN 1 WHEN 'force_out' THEN 1
            WHEN 'sac_fly' THEN 1 WHEN 'sac_bunt' THEN 1 WHEN 'fielders_choice_out' THEN 1
            WHEN 'fielders_choice' THEN 1 WHEN 'other_out' THEN 1
            WHEN 'strikeout_double_play' THEN 2 WHEN 'grounded_into_double_play' THEN 2
            WHEN 'double_play' THEN 2 WHEN 'sac_fly_double_play' THEN 2
            WHEN 'sac_bunt_double_play' THEN 2 WHEN 'triple_play' THEN 3
            ELSE 0
        END AS outs
    FROM statcast_pitches
    WHERE inning_topbot IS NOT NULL
),
first_pitcher AS (
    SELECT game_pk, pitcher_team, pitcher,
        ROW_NUMBER() OVER (PARTITION BY game_pk, pitcher_team
                           ORDER BY inning, at_bat_number, pitch_number) AS rn
    FROM gp
),
starters AS (SELECT DISTINCT game_pk, pitcher_team, pitcher FROM first_pitcher WHERE rn = 1),
team_games AS (
    -- Rank each team's games most-recent first; keep the last 10.
    SELECT pitcher_team AS team, game_pk, game_date,
        DENSE_RANK() OVER (PARTITION BY pitcher_team ORDER BY game_date DESC, game_pk DESC) AS gnum
    FROM (SELECT DISTINCT pitcher_team, game_pk, game_date FROM gp) d
),
last10 AS (SELECT team, game_pk FROM team_games WHERE gnum <= 10),
relief AS (
    SELECT g.*
    FROM gp g
    JOIN last10 l ON l.team = g.pitcher_team AND l.game_pk = g.game_pk
    LEFT JOIN starters s
      ON s.game_pk = g.game_pk AND s.pitcher = g.pitcher AND s.pitcher_team = g.pitcher_team
    WHERE s.pitcher IS NULL   -- exclude the starter => relievers only
)
SELECT
    pitcher_team AS team,
    round(sum(outs) / 3.0, 1) AS bullpen_ip,
    sum(runs_on_play) AS runs_allowed,
    round(sum(runs_on_play) * 9.0 / NULLIF(sum(outs) / 3.0, 0), 2) AS bullpen_ra9,
    round((count(*) FILTER (WHERE events IN ('single','double','triple','home_run'))
           + count(*) FILTER (WHERE events = 'walk'))::numeric
          / NULLIF(sum(outs) / 3.0, 0), 2) AS whip,
    round(count(*) FILTER (WHERE events = 'strikeout') * 9.0
          / NULLIF(sum(outs) / 3.0, 0), 2) AS k9
FROM relief
GROUP BY pitcher_team;

CREATE INDEX IF NOT EXISTS idx_mv_bullpen_team ON mv_bullpen_l10(team);

-- ---------------------------------------------------------------------------
-- 5. v_team_platoon_ops — Team OPS vs RHP / vs LHP (regular view)
-- ---------------------------------------------------------------------------
DROP VIEW IF EXISTS v_team_platoon_ops CASCADE;
CREATE VIEW v_team_platoon_ops AS
WITH pa AS (
    SELECT
        CASE WHEN inning_topbot = 'Top' THEN away_team ELSE home_team END AS team,
        p_throws, events
    FROM statcast_pitches
    WHERE events IS NOT NULL AND events <> '' AND inning_topbot IS NOT NULL
),
agg AS (
    SELECT team, p_throws,
        count(*) FILTER (WHERE events IN ('single','double','triple','home_run')) AS h,
        count(*) FILTER (WHERE events = 'walk') AS bb,
        count(*) FILTER (WHERE events = 'hit_by_pitch') AS hbp,
        count(*) FILTER (WHERE events IN ('sac_fly','sac_fly_double_play')) AS sf,
        count(*) FILTER (WHERE events NOT IN
            ('walk','hit_by_pitch','sac_fly','sac_bunt','catcher_interf',
             'sac_fly_double_play','sac_bunt_double_play')) AS ab,
        (count(*) FILTER (WHERE events = 'single')
         + 2 * count(*) FILTER (WHERE events = 'double')
         + 3 * count(*) FILTER (WHERE events = 'triple')
         + 4 * count(*) FILTER (WHERE events = 'home_run')) AS tb
    FROM pa
    GROUP BY team, p_throws
)
SELECT
    team,
    max(CASE WHEN p_throws = 'R' THEN
        round((h + bb + hbp)::numeric / NULLIF(ab + bb + hbp + sf, 0)
              + tb::numeric / NULLIF(ab, 0), 3) END) AS ops_vs_rhp,
    max(CASE WHEN p_throws = 'L' THEN
        round((h + bb + hbp)::numeric / NULLIF(ab + bb + hbp + sf, 0)
              + tb::numeric / NULLIF(ab, 0), 3) END) AS ops_vs_lhp
FROM agg
GROUP BY team;
