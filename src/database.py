# src/database.py
import os
import json
import time
import re
import secrets
import psycopg2
import psycopg2.extensions
from psycopg2.extras import RealDictCursor, Json
from psycopg2.pool import ThreadedConnectionPool
from pathlib import Path
from datetime import datetime, timezone, date
import traceback
from src.config import CONFIG, Style
from src.perf import timed_sync
from src.cache import track_question_cache
from src.geo import get_timezone_for_country
import asyncio as _asyncio
from src.cache import TTLCache
user_profile_cache = TTLCache(default_ttl=8.0)
DB_SEMAPHORE = _asyncio.Semaphore(int(os.getenv("DB_MAX_INFLIGHT", "80")))
_FN_PROCESS_USER_SCORE_SQL = """
CREATE OR REPLACE FUNCTION fn_process_user_score(
    p_user_id text, p_message_id text, p_q_id text, p_is_correct boolean,
    p_selected_option int, p_private_message_id bigint,
    p_show_derivation boolean, p_show_perf boolean
) RETURNS TABLE (
    o_total int, o_correct int, o_total_marks int, o_marks_awarded int,
    o_first_try boolean, o_speed_tier text, o_grade int, o_current_streak int
) AS $$
DECLARE
    v_existing_marks int; v_marks int := 0; v_first_try boolean := true;
    v_streak int := 0; v_streak_mult numeric := 1.0; v_last_active timestamptz;
    v_last_active_date date; v_today date := (NOW() AT TIME ZONE 'utc')::date;
    v_days_diff int; v_user_grade int; v_question_grade int; v_q_grade_raw text;
    v_difficulty text; v_subject text; v_base_marks numeric; v_speed_mult numeric;
    v_speed_tier text; v_grade_mult numeric; v_sent_at timestamptz; v_seconds_since_sent numeric;
    v_referrer_t1 text; v_referrer_t2 text; v_t1_bonus int; v_t2_bonus int; v_potential_marks int;
    v_city text; v_country text;
BEGIN
    SELECT ur.marks_awarded INTO v_existing_marks FROM user_responses ur
     WHERE ur.user_id = p_user_id AND ur.message_id = p_message_id;

    IF FOUND THEN
        v_first_try := false;
        v_marks := v_existing_marks;
    ELSE
        SELECT us.last_active_at, COALESCE(us.current_streak, 0), us.grade, us.referred_by,
               us.personal_city, us.personal_country
          INTO v_last_active, v_streak, v_user_grade, v_referrer_t1, v_city, v_country
          FROM user_stats us WHERE us.user_id = p_user_id;

        IF v_last_active IS NULL THEN
            v_streak := 1;
        ELSE
            v_last_active_date := v_last_active::date;
            v_days_diff := v_today - v_last_active_date;
            IF v_days_diff = 1 THEN v_streak := v_streak + 1;
            ELSIF v_days_diff > 1 THEN v_streak := 1;
            END IF;
        END IF;

        IF v_streak >= 7 THEN v_streak_mult := 1.5;
        ELSIF v_streak >= 3 THEN v_streak_mult := 1.2;
        ELSE v_streak_mult := 1.0;
        END IF;

        SELECT q.difficulty, q.subject, q.tags::text INTO v_difficulty, v_subject, v_q_grade_raw
          FROM questions q WHERE q.id = p_q_id;

        v_base_marks := CASE lower(COALESCE(v_difficulty, 'medium'))
            WHEN 'easy' THEN 3 WHEN 'weak' THEN 3 WHEN 'hard' THEN 12 ELSE 6 END;

        SELECT st.sent_at INTO v_sent_at FROM sent_tracks st WHERE st.message_id = p_message_id;
        v_seconds_since_sent := EXTRACT(EPOCH FROM (NOW() - COALESCE(v_sent_at, NOW())));

        IF v_seconds_since_sent <= 60 THEN v_speed_mult := 1.5; v_speed_tier := 'lightning';
        ELSIF v_seconds_since_sent <= 300 THEN v_speed_mult := 1.2; v_speed_tier := 'fast';
        ELSE v_speed_mult := 1.0; v_speed_tier := 'standard';
        END IF;

        IF v_q_grade_raw LIKE '%grade6%' THEN v_question_grade := 6;
        ELSIF v_q_grade_raw LIKE '%grade8%' THEN v_question_grade := 8;
        ELSIF v_q_grade_raw LIKE '%grade10%' THEN v_question_grade := 10;
        ELSIF v_q_grade_raw LIKE '%grade12%' THEN v_question_grade := 12;
        ELSE v_question_grade := COALESCE(v_user_grade, 12);
        END IF;

        IF v_user_grade IS NULL THEN v_grade_mult := 1.0;
        ELSIF v_user_grade < v_question_grade THEN v_grade_mult := 1.5;
        ELSIF v_user_grade > v_question_grade THEN v_grade_mult := 0.3;
        ELSE v_grade_mult := 1.0;
        END IF;

        v_potential_marks := GREATEST(1, FLOOR(v_base_marks * v_speed_mult * v_grade_mult * v_streak_mult)::int);
        v_marks := CASE WHEN p_is_correct THEN v_potential_marks ELSE 0 END;

        BEGIN
            INSERT INTO user_stats (user_id, total, correct, total_marks, current_streak, last_active_at)
            VALUES (p_user_id, 1, CASE WHEN p_is_correct THEN 1 ELSE 0 END, v_marks, v_streak, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                total = COALESCE(user_stats.total, 0) + 1,
                correct = COALESCE(user_stats.correct, 0) + CASE WHEN p_is_correct THEN 1 ELSE 0 END,
                total_marks = COALESCE(user_stats.total_marks, 0) + v_marks,
                current_streak = v_streak, last_active_at = NOW();

            INSERT INTO user_responses (
                user_id, message_id, q_id, is_correct, marks_awarded,
                selected_option, private_message_id, show_derivation, show_perf, potential_marks
            ) VALUES (
                p_user_id, p_message_id, p_q_id, p_is_correct, v_marks,
                p_selected_option, p_private_message_id, p_show_derivation, p_show_perf, v_potential_marks
            );

            IF p_is_correct AND v_marks > 0 AND v_subject IS NOT NULL THEN
                INSERT INTO user_subject_marks (user_id, subject, marks)
                VALUES (p_user_id, lower(v_subject), v_marks)
                ON CONFLICT (user_id, subject) DO UPDATE SET marks = user_subject_marks.marks + v_marks;
            END IF;

            IF p_is_correct AND v_marks > 0 AND v_difficulty IS NOT NULL THEN
                INSERT INTO user_difficulty_marks (user_id, difficulty, marks)
                VALUES (p_user_id, lower(CASE WHEN lower(v_difficulty) = 'weak' THEN 'easy' ELSE v_difficulty END), v_marks)
                ON CONFLICT (user_id, difficulty) DO UPDATE SET marks = user_difficulty_marks.marks + v_marks;
            END IF;

            -- Team contribution: split evenly across every team this user is CURRENTLY
            -- an active member of. A team a user has since LEFT never receives another
            -- cent here — its row is frozen at whatever it accumulated while they were in.
            IF p_is_correct AND v_marks > 0 THEN
                INSERT INTO user_org_contributions (user_id, org_id, marks)
                SELECT p_user_id, m.org_id, v_marks::numeric / GREATEST(1, cnt.active_count)
                FROM org_memberships m
                CROSS JOIN (
                    SELECT COUNT(*) AS active_count FROM org_memberships
                    WHERE user_id = p_user_id AND org_role NOT IN ('pending','rejected','left')
                ) cnt
                WHERE m.user_id = p_user_id AND m.org_role NOT IN ('pending','rejected','left')
                ON CONFLICT (user_id, org_id) DO UPDATE SET marks = user_org_contributions.marks + EXCLUDED.marks;

                IF v_city IS NOT NULL THEN
                    INSERT INTO user_geo_contributions (user_id, geo_type, geo_value, marks)
                    VALUES (p_user_id, 'city', v_city, v_marks)
                    ON CONFLICT (user_id, geo_type, geo_value) DO UPDATE SET marks = user_geo_contributions.marks + v_marks;
                END IF;
                IF v_country IS NOT NULL THEN
                    INSERT INTO user_geo_contributions (user_id, geo_type, geo_value, marks)
                    VALUES (p_user_id, 'country', v_country, v_marks)
                    ON CONFLICT (user_id, geo_type, geo_value) DO UPDATE SET marks = user_geo_contributions.marks + v_marks;
                END IF;
            END IF;

            IF p_is_correct AND v_marks > 0 AND v_referrer_t1 IS NOT NULL THEN
                v_t1_bonus := GREATEST(1, FLOOR(v_marks * 0.05)::int);
                UPDATE user_stats SET total_marks = COALESCE(total_marks, 0) + v_t1_bonus WHERE user_id = v_referrer_t1;
                SELECT referred_by INTO v_referrer_t2 FROM user_stats WHERE user_id = v_referrer_t1;
                IF v_referrer_t2 IS NOT NULL THEN
                    v_t2_bonus := GREATEST(1, FLOOR(v_marks * 0.025)::int);
                    UPDATE user_stats SET total_marks = COALESCE(total_marks, 0) + v_t2_bonus WHERE user_id = v_referrer_t2;
                END IF;
            END IF;
        EXCEPTION WHEN unique_violation THEN
            v_first_try := false;
            SELECT ur.marks_awarded INTO v_existing_marks FROM user_responses ur
             WHERE ur.user_id = p_user_id AND ur.message_id = p_message_id;
            v_marks := COALESCE(v_existing_marks, 0);
        END;
    END IF;

    RETURN QUERY
    SELECT us.total, us.correct, us.total_marks, v_marks, v_first_try, v_speed_tier, us.grade, us.current_streak
      FROM user_stats us WHERE us.user_id = p_user_id;
END;
$$ LANGUAGE plpgsql;
"""

_FN_EDIT_USER_ANSWER_SQL = """
CREATE OR REPLACE FUNCTION fn_edit_user_answer(
    p_user_id text, p_message_id text, p_new_option int, p_new_is_correct boolean
) RETURNS TABLE (o_total_marks int, o_marks_awarded int, o_edit_count int, o_result_flip text) AS $$
DECLARE
    v_row RECORD;
    v_potential int; v_old_marks int; v_new_marks int; v_delta int;
    v_first_correct boolean; v_flip text;
    v_referrer_t1 text; v_referrer_t2 text; v_delta_t1 int; v_delta_t2 int;
BEGIN
    SELECT * INTO v_row FROM user_responses WHERE user_id = p_user_id AND message_id = p_message_id FOR UPDATE;
    IF NOT FOUND THEN RETURN; END IF;

    v_potential := COALESCE(v_row.potential_marks, v_row.marks_awarded, 0);
    v_old_marks := COALESCE(v_row.marks_awarded, 0);
    v_new_marks := CASE WHEN p_new_is_correct THEN v_potential ELSE 0 END;
    v_delta := v_new_marks - v_old_marks;
    v_first_correct := COALESCE(v_row.first_is_correct, v_row.is_correct);

    v_flip := CASE
        WHEN v_first_correct AND NOT p_new_is_correct THEN 'hurt'
        WHEN NOT v_first_correct AND p_new_is_correct THEN 'helped'
        ELSE 'neutral' END;

    UPDATE user_responses SET
        selected_option = p_new_option, is_correct = p_new_is_correct, marks_awarded = v_new_marks,
        edit_count = COALESCE(edit_count, 0) + 1,
        first_selected_option = COALESCE(first_selected_option, v_row.selected_option),
        first_is_correct = COALESCE(first_is_correct, v_row.is_correct)
    WHERE user_id = p_user_id AND message_id = p_message_id;

    UPDATE user_stats SET
        total_marks = COALESCE(total_marks, 0) + v_delta,
        correct = COALESCE(correct, 0) + (CASE WHEN p_new_is_correct AND NOT v_row.is_correct THEN 1
                                                WHEN NOT p_new_is_correct AND v_row.is_correct THEN -1 ELSE 0 END),
        answer_edits_total = COALESCE(answer_edits_total, 0) + 1,
        answer_edits_helped = COALESCE(answer_edits_helped, 0) + (CASE WHEN v_flip = 'helped' THEN 1 ELSE 0 END),
        answer_edits_hurt = COALESCE(answer_edits_hurt, 0) + (CASE WHEN v_flip = 'hurt' THEN 1 ELSE 0 END)
    WHERE user_id = p_user_id;

    IF v_delta != 0 THEN
        UPDATE user_subject_marks usm SET marks = usm.marks + v_delta
        FROM questions q WHERE q.id = v_row.q_id AND lower(q.subject) = usm.subject AND usm.user_id = p_user_id;
    END IF;

    SELECT referred_by INTO v_referrer_t1 FROM user_stats WHERE user_id = p_user_id;
    IF v_referrer_t1 IS NOT NULL AND v_delta != 0 THEN
        v_delta_t1 := CEIL(v_delta * 0.05);
        UPDATE user_stats SET total_marks = COALESCE(total_marks,0) + v_delta_t1 WHERE user_id = v_referrer_t1;
        SELECT referred_by INTO v_referrer_t2 FROM user_stats WHERE user_id = v_referrer_t1;
        IF v_referrer_t2 IS NOT NULL THEN
            v_delta_t2 := CEIL(v_delta * 0.025);
            UPDATE user_stats SET total_marks = COALESCE(total_marks,0) + v_delta_t2 WHERE user_id = v_referrer_t2;
        END IF;
    END IF;

    RETURN QUERY SELECT us.total_marks, v_new_marks, ur.edit_count, v_flip
        FROM user_stats us JOIN user_responses ur ON ur.user_id = us.user_id
        WHERE us.user_id = p_user_id AND ur.message_id = p_message_id;
END; $$ LANGUAGE plpgsql;
"""

class QuizEngine:
    _pool = None
    _warned_detected = False
    _tracks_cache = {}
    _tracks_cache_time = 0
    _fn_ensured = False
    _tournament_schema_ensured = False

    def __init__(self):
        self.config = CONFIG
        self.db = {}
        self.last_refresh = 0
        self.refresh_interval = 30
        self.db_url = self.config.get("database_url")
        if self.db_url:
            if not QuizEngine._pool:
                try:
                    prewarm_count = int(os.getenv("DB_POOL_PREWARM", "10"))
                    max_count = int(os.getenv("DB_POOL_MAX", "20"))
                    prewarm_count = min(prewarm_count, max_count)

                    t0 = time.perf_counter()
                    QuizEngine._pool = ThreadedConnectionPool(
                        minconn=prewarm_count,
                        maxconn=max_count,
                        dsn=self.db_url
                    )
                    warm_ms = (time.perf_counter() - t0) * 1000
                    if not QuizEngine._warned_detected:
                        print(f"{Style.GREEN}[DATABASE] Threaded connection pool initialized "
                              f"({prewarm_count} pre-warmed / {max_count} max) in {warm_ms:.0f}ms.{Style.RESET}")
                        QuizEngine._warned_detected = True
                except Exception as e:
                    print(f"{Style.RED}[DATABASE ERROR] Failed to initialize connection pool: {e}{Style.RESET}")

            self._ensure_functions()
            self._ensure_tournament_schema()
        else:
            print(f"{Style.YELLOW}[DATABASE] Running without cloud database environment.{Style.RESET}")

    def _ensure_functions(self):
        if QuizEngine._fn_ensured:
            return
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                print("[DATABASE] Dropping obsolete conflicting definitions of fn_process_user_score...", flush=True)
                cur.execute("DROP FUNCTION IF EXISTS fn_process_user_score(text, text, text, boolean, integer, integer, boolean, boolean, integer);")
                cur.execute("DROP FUNCTION IF EXISTS fn_process_user_score(text, text, text, boolean, integer, bigint, boolean, boolean, integer);")
                cur.execute("DROP FUNCTION IF EXISTS fn_process_user_score(text, text, text, boolean, integer, bigint, boolean, boolean);")
                cur.execute("DROP FUNCTION IF EXISTS fn_process_user_score(text, text, text, boolean, integer, integer, boolean, boolean);")

                cur.execute(_FN_PROCESS_USER_SCORE_SQL)
                cur.execute(_FN_EDIT_USER_ANSWER_SQL)
                conn.commit()
            QuizEngine._fn_ensured = True
            print(f"{Style.GREEN}[DATABASE] fn_process_user_score ensured.{Style.RESET}")
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"{Style.RED}[DATABASE ERROR] Failed to ensure fn_process_user_score: {e}{Style.RESET}")
        finally:
            if conn:
                self.release_connection(conn)

    def _ensure_tournament_schema(self):
        if QuizEngine._tournament_schema_ensured:
            return

        # Isolated: pgcrypto may be restricted on some managed Postgres tiers (e.g. Neon
        # free/shared). A failure here must NEVER block the rest of schema setup below,
        # since token generation is optional but the feedback/org tables are not.
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
            print(f"{Style.GREEN}[DATABASE] pgcrypto extension ensured.{Style.RESET}")
        except Exception as e:
            print(f"{Style.YELLOW}[DATABASE WARNING] pgcrypto extension unavailable (tokens will fall back to Python-generated hex): {e}{Style.RESET}")
        finally:
            if conn:
                self.release_connection(conn)

        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM sent_tracks LIMIT 1;")
                cur.execute("SELECT 1 FROM tournament_queue LIMIT 1;")

                # Tags each round with which tournament run it belongs to, so scores can be
                # aggregated strictly within one tournament series instead of lifetime totals.
                cur.execute("ALTER TABLE sent_tracks ADD COLUMN IF NOT EXISTS tournament_run_id TEXT;")

                # Setup Organizations Tables with geographic properties
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS organizations (
                        org_id SERIAL PRIMARY KEY,
                        org_name VARCHAR(50) NOT NULL,
                        org_tag VARCHAR(15) UNIQUE NOT NULL,
                        creator_id VARCHAR(20) NOT NULL,
                        org_type VARCHAR(20) DEFAULT 'School',
                        is_public BOOLEAN DEFAULT TRUE,
                        city VARCHAR(50) DEFAULT 'Addis Ababa',
                        country VARCHAR(50) DEFAULT 'Ethiopia',
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)

                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS username text;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS first_name text;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS nickname text;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS public_consent_granted BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE;")

                # Add geographic fallbacks to student stats
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS personal_city VARCHAR(50) DEFAULT 'Addis Ababa';")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS personal_country VARCHAR(50) DEFAULT 'Ethiopia';")

                # Add dynamic MLM referral invite tag column
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS referred_by VARCHAR(20) REFERENCES user_stats(user_id) ON DELETE SET NULL;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) DEFAULT 'UTC';")
                # Setup Dynamic Many-to-Many Membership table
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS org_memberships (
                        user_id VARCHAR(20) NOT NULL,
                        org_id INTEGER REFERENCES organizations(org_id) ON DELETE CASCADE,
                        org_role VARCHAR(15) DEFAULT 'member',
                        joined_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (user_id, org_id)
                    );
                """)

                # Port historical school teams from stats table if column exists
                try:
                    cur.execute("SELECT org_id FROM user_stats LIMIT 1;")
                    has_legacy_col = True
                except Exception:
                    has_legacy_col = False
                    conn.rollback()

                if has_legacy_col:
                    cur.execute("""
                        INSERT INTO org_memberships (user_id, org_id, org_role)
                        SELECT user_id, org_id, org_role FROM user_stats WHERE org_id IS NOT NULL
                        ON CONFLICT DO NOTHING;
                    """)
                    cur.execute("ALTER TABLE user_stats DROP COLUMN IF EXISTS org_id CASCADE;")
                    cur.execute("ALTER TABLE user_stats DROP COLUMN IF EXISTS org_role CASCADE;")

                # Check for is_public column inside organizations
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS is_public BOOLEAN DEFAULT TRUE;")
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS city VARCHAR(50) DEFAULT 'Addis Ababa';")
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS country VARCHAR(50) DEFAULT 'Ethiopia';")

                # Secure, unguessable invite/join tokens — generated in Python, not via
                # gen_random_bytes(), so this never depends on the pgcrypto extension.
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS join_token VARCHAR(32) UNIQUE;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS referral_token VARCHAR(32) UNIQUE;")

                cur.execute("SELECT org_id FROM organizations WHERE join_token IS NULL;")
                for row in cur.fetchall():
                    cur.execute("UPDATE organizations SET join_token = %s WHERE org_id = %s;", (secrets.token_hex(16), row["org_id"]))

                cur.execute("SELECT user_id FROM user_stats WHERE referral_token IS NULL;")
                for row in cur.fetchall():
                    cur.execute("UPDATE user_stats SET referral_token = %s WHERE user_id = %s;", (secrets.token_hex(16), row["user_id"]))

                cur.execute("ALTER TABLE user_responses DROP CONSTRAINT IF EXISTS user_responses_message_id_fkey;")
                cur.execute("ALTER TABLE user_responses ADD COLUMN IF NOT EXISTS edit_count INT DEFAULT 0;")
                cur.execute("ALTER TABLE user_responses ADD COLUMN IF NOT EXISTS first_selected_option INT;")
                cur.execute("ALTER TABLE user_responses ADD COLUMN IF NOT EXISTS first_is_correct BOOLEAN;")
                cur.execute("ALTER TABLE user_responses ADD COLUMN IF NOT EXISTS potential_marks INT;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS answer_edits_total INT DEFAULT 0;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS answer_edits_helped INT DEFAULT 0;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS answer_edits_hurt INT DEFAULT 0;")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback (
                        id SERIAL PRIMARY KEY,
                        user_id VARCHAR(20) NOT NULL,
                        category VARCHAR(20) NOT NULL,
                        message TEXT NOT NULL,
                        status VARCHAR(20) DEFAULT 'open',
                        admin_reply TEXT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS feedback_messages (
                        id SERIAL PRIMARY KEY,
                        feedback_id INTEGER REFERENCES feedback(id) ON DELETE CASCADE,
                        sender_role VARCHAR(10) NOT NULL,
                        sender_user_id VARCHAR(20),
                        message TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("ALTER TABLE sent_tracks ADD COLUMN IF NOT EXISTS sent_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_subject_marks (
                        user_id VARCHAR(20) NOT NULL,
                        subject VARCHAR(50) NOT NULL,
                        marks INT DEFAULT 0,
                        PRIMARY KEY (user_id, subject)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_difficulty_marks (
                        user_id VARCHAR(20) NOT NULL,
                        difficulty VARCHAR(20) NOT NULL,
                        marks INT DEFAULT 0,
                        PRIMARY KEY (user_id, difficulty)
                    );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_stats_grade ON user_stats(grade);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_org_memberships_org_role ON org_memberships(org_id, org_role);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_subject_marks_subject ON user_subject_marks(subject);")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_user_difficulty_marks_diff ON user_difficulty_marks(difficulty);")
                cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS last_shown_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS times_shown INT DEFAULT 0;")
                cur.execute("ALTER TABLE questions ADD COLUMN IF NOT EXISTS first_shown_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE org_memberships ADD COLUMN IF NOT EXISTS request_count INT DEFAULT 1;")
                cur.execute("ALTER TABLE org_memberships ADD COLUMN IF NOT EXISTS last_requested_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE channel_campaigns ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;")
                cur.execute("ALTER TABLE org_memberships ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;")
                # One-time backfill: earliest send per question becomes its first_shown_at
                cur.execute("""
                    UPDATE questions q SET first_shown_at = sub.min_sent
                    FROM (
                        SELECT q_id, MIN(sent_at) AS min_sent
                        FROM sent_tracks GROUP BY q_id
                    ) sub
                    WHERE q.id = sub.q_id AND q.first_shown_at IS NULL;
                """)
                # One-time backfill from existing send history so cooldown works immediately
                cur.execute("""
                    UPDATE questions q SET last_shown_at = sub.max_sent, times_shown = sub.cnt
                    FROM (
                        SELECT q_id, MAX(sent_at) AS max_sent, COUNT(*) AS cnt
                        FROM sent_tracks GROUP BY q_id
                    ) sub
                    WHERE q.id = sub.q_id AND q.last_shown_at IS NULL;
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS bot_state (
                        key TEXT PRIMARY KEY,
                        value JSONB
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS channel_campaigns (
                        id SERIAL PRIMARY KEY,
                        name TEXT UNIQUE NOT NULL,
                        html_content TEXT NOT NULL,
                        is_active BOOLEAN DEFAULT TRUE,
                        pin_it BOOLEAN DEFAULT TRUE,
                        schedule JSONB DEFAULT '{"enabled": false}'::jsonb,
                        posted_mid BIGINT,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS status VARCHAR(15) DEFAULT 'approved';")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS personal_city_status VARCHAR(15) DEFAULT 'approved';")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS pending_city_suggestion_id INT;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS show_real_identity BOOLEAN DEFAULT FALSE;")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS location_suggestions (
                        id SERIAL PRIMARY KEY,
                        kind VARCHAR(10) NOT NULL,
                        name VARCHAR(80) NOT NULL,
                        normalized_name VARCHAR(80) NOT NULL,
                        country VARCHAR(50),
                        submitted_by VARCHAR(20) NOT NULL,
                        status VARCHAR(15) DEFAULT 'pending',
                        org_id INT,
                        admin_id VARCHAR(20),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        resolved_at TIMESTAMPTZ
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS location_suggestion_messages (
                        id SERIAL PRIMARY KEY,
                        suggestion_id INT REFERENCES location_suggestions(id) ON DELETE CASCADE,
                        sender_role VARCHAR(10) NOT NULL,
                        sender_user_id VARCHAR(20),
                        message TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW()
                    );
                """)
                cur.execute("ALTER TABLE location_suggestions ADD COLUMN IF NOT EXISTS request_count INT DEFAULT 1;")
                cur.execute("ALTER TABLE location_suggestions ADD COLUMN IF NOT EXISTS last_requested_at TIMESTAMPTZ DEFAULT NOW();")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_hidden_questions (
                        user_id VARCHAR(20) NOT NULL,
                        q_id TEXT NOT NULL,
                        hidden_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (user_id, q_id)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_org_contributions (
                        user_id VARCHAR(20) NOT NULL,
                        org_id INTEGER NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
                        marks NUMERIC DEFAULT 0,
                        PRIMARY KEY (user_id, org_id)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_geo_contributions (
                        user_id VARCHAR(20) NOT NULL,
                        geo_type VARCHAR(10) NOT NULL,
                        geo_value VARCHAR(80) NOT NULL,
                        marks NUMERIC DEFAULT 0,
                        PRIMARY KEY (user_id, geo_type, geo_value)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS school_branches (
                        branch_id SERIAL PRIMARY KEY,
                        org_id INTEGER NOT NULL REFERENCES organizations(org_id) ON DELETE CASCADE,
                        branch_name VARCHAR(80) NOT NULL,
                        city VARCHAR(50),
                        country VARCHAR(50),
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        deleted_at TIMESTAMPTZ,
                        UNIQUE (org_id, branch_name)
                    );
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_favorites (
                        user_id VARCHAR(20) NOT NULL,
                        fav_type VARCHAR(10) NOT NULL,
                        fav_value VARCHAR(100) NOT NULL,
                        fav_label VARCHAR(120) NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        PRIMARY KEY (user_id, fav_type, fav_value)
                    );
                """)
                cur.execute("ALTER TABLE org_memberships ADD COLUMN IF NOT EXISTS branch_id INTEGER REFERENCES school_branches(branch_id) ON DELETE SET NULL;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS branch_id INTEGER REFERENCES school_branches(branch_id) ON DELETE SET NULL;")
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS team_scope VARCHAR(10) DEFAULT 'open';")
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS scope_value VARCHAR(80);")
                cur.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS description TEXT;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS last_utility_mid BIGINT;")
                conn.commit()

            QuizEngine._tournament_schema_ensured = True
            print(f"{Style.GREEN}[DATABASE] Many-to-many organizations and roster schemas verified.{Style.RESET}")
            print(f"{Style.GREEN}[DEBUG-SCHEMA-FIX] Dropped restrictive FK constraints.{Style.RESET}", flush=True)
        except Exception as e:
            if conn:
                conn.rollback()
            print(f"{Style.YELLOW}[DATABASE WARNING] Schema checks encountered: {e}. Ensure migrations have run.{Style.RESET}")
        finally:
            if conn:
                self.release_connection(conn)

    def get_db_connection(self):
        if not self.db_url:
            raise ConnectionError("DATABASE_URL environment variable is missing.")

        if QuizEngine._pool:
            for _ in range(3):
                try:
                    conn = QuizEngine._pool.getconn()
                    # FORCE AUTOCOMMIT TO PREVENT STALE TRANSACTION LOCKS & LEAKS ACROSS DUAL PROCESSES
                    conn.autocommit = True
                    conn.cursor_factory = RealDictCursor

                    if conn.closed == 0:
                        return conn
                    else:
                        try:
                            QuizEngine._pool.putconn(conn, close=True)
                        except Exception:
                            pass
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    print(f"{Style.YELLOW}[DATABASE] Discarding stale connection from pool: {e}{Style.RESET}")
                    try:
                        QuizEngine._pool.putconn(conn, close=True)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"{Style.YELLOW}[DATABASE WARNING] Connection pool checkout failed: {e}{Style.RESET}")
                    break

        conn = psycopg2.connect(
            self.db_url,
            cursor_factory=RealDictCursor,
            connect_timeout=5
        )
        conn.autocommit = True
        return conn

    def release_connection(self, conn):
        if not conn:
            return
        if QuizEngine._pool:
            try:
                try:
                    conn.rollback()
                except Exception:
                    pass

                if conn.closed != 0:
                    QuizEngine._pool.putconn(conn, close=True)
                else:
                    QuizEngine._pool.putconn(conn)
                return
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass

    def db_get_current_epoch(self) -> float:
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT EXTRACT(EPOCH FROM NOW()) AS now_epoch;")
                row = cur.fetchone()
                return float(row['now_epoch']) if row else time.time()
        except Exception:
            return time.time()
        finally:
            if conn:
                self.release_connection(conn)

    @timed_sync(lambda self, message_id, *a, **kw: f"db_save_track(msg={message_id})")
    def db_save_track(self, message_id, q_id, status, display_id, type_, msg_type, followup_mid=None, round_deadline=None, round_seconds=None):
        QuizEngine._tracks_cache_time = 0
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                if round_seconds is not None:
                    cur.execute("""
                        INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, followup_mid, round_deadline)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW() + (%s || ' second')::interval)
                        ON CONFLICT (message_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            followup_mid = EXCLUDED.followup_mid,
                            round_deadline = EXCLUDED.round_deadline;
                    """, (str(message_id), q_id, status, int(display_id), type_, msg_type, followup_mid, int(round_seconds)))
                else:
                    cur.execute("""
                        INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, followup_mid, round_deadline)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (message_id) DO UPDATE SET
                            status = EXCLUDED.status,
                            followup_mid = EXCLUDED.followup_mid,
                            round_deadline = EXCLUDED.round_deadline;
                    """, (str(message_id), q_id, status, int(display_id), type_, msg_type, followup_mid, round_deadline))
                conn.commit()
            track_question_cache.invalidate(f"trackq:{display_id}")
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to save track: {e}{Style.RESET}")
        finally:
            if conn:
                self.release_connection(conn)

    @timed_sync(lambda self, message_id, followup_mid, msg_type: f"db_update_track_followup_and_type")
    def db_update_track_followup_and_type(self, message_id, followup_mid, msg_type):
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sent_tracks SET followup_mid = %s, msg_type = %s WHERE message_id = %s;",
                    (followup_mid, msg_type, str(message_id))
                )
                conn.commit()
        except Exception as e:
            if conn: conn.rollback()
            print(f"[DB ERROR] Failed to update track metadata: {e}")
        finally:
            if conn:
                self.release_connection(conn)

    @timed_sync(lambda self, message_id: f"db_delete_track")
    def db_delete_track(self, message_id):
        conn = None
        try:
            conn = GLOBAL_ENGINE.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (str(message_id),))
                conn.commit()
        except Exception as e:
            if conn: conn.rollback()
            print(f"[DB ERROR] Failed to delete track: {e}")
        finally:
            if conn:
                self.release_connection(conn)

    @timed_sync(lambda self: "db_get_all_tracks")
    def db_get_all_tracks(self):
        now = time.time()
        if QuizEngine._tracks_cache and (now - QuizEngine._tracks_cache_time < 5):
            return QuizEngine._tracks_cache

        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM sent_tracks;")
                rows = cur.fetchall()
                QuizEngine._tracks_cache = {r['message_id']: dict(r) for r in rows}
                QuizEngine._tracks_cache_time = now
                return QuizEngine._tracks_cache
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to retrieve tracks: {e}{Style.RESET}")
            return {}
        finally:
            if conn:
                self.release_connection(conn)

    @timed_sync(lambda self, message_id, status, *a, **kw: f"db_update_track_status(msg={message_id})")
    def db_update_track_status(self, message_id, status, followup_mid=None, clear_followup=False):
        QuizEngine._tracks_cache_time = 0
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                if clear_followup:
                    cur.execute("UPDATE sent_tracks SET status = %s, followup_mid = NULL WHERE message_id = %s;", (status, str(message_id)))
                elif followup_mid is not None:
                    cur.execute("UPDATE sent_tracks SET status = %s, followup_mid = %s WHERE message_id = %s;", (status, followup_mid, str(message_id)))
                else:
                    cur.execute("UPDATE sent_tracks SET status = %s WHERE message_id = %s;", (status, str(message_id)))
                conn.commit()
            track_question_cache.invalidate_prefix("trackq:")
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to update track status: {e}{Style.RESET}")
        finally:
            if conn:
                self.release_connection(conn)

    @timed_sync(lambda self, old_mid, new_mid: f"db_swap_track_message_id({old_mid}->{new_mid})")
    def db_swap_track_message_id(self, old_mid, new_mid):
        QuizEngine._tracks_cache_time = 0
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                try:
                    cur.execute("UPDATE user_responses SET message_id = %s WHERE message_id = %s;", (str(new_mid), str(old_mid)))
                    print(f"[DEBUG-DB-SWAP] Shifted answers in user_responses {old_mid} -> {new_mid}. Affected: {cur.rowcount}", flush=True)
                except Exception as e:
                    print(f"[DB SWAP WARNING] user_responses update bypassed: {e}")

                cur.execute("UPDATE sent_tracks SET message_id = %s WHERE message_id = %s;", (str(new_mid), str(old_mid)))
                print(f"[DEBUG-DB-SWAP] Shifted tracks in sent_tracks {old_mid} -> {new_mid}. Affected: {cur.rowcount}", flush=True)
                conn.commit()
            track_question_cache.invalidate_prefix("trackq:")
        except Exception as e:
            if conn: conn.rollback()
            print(f"[DB ERROR] Failed to swap track message ID: {e}")
        finally:
            if conn:
                self.release_connection(conn)

    @timed_sync(lambda self, json_data: "db_import_questions")
    def db_import_questions(self, json_data):
        conn = None
        self._last_import_repeats = []
        try:
            questions_list = json_data if isinstance(json_data, list) else [json_data]
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                imported_count = 0
                repeat_alerts = []

                for q in questions_list:
                    if not q.get("id") or not q.get("subject"):
                        continue

                    # Check prior history BEFORE the upsert overwrites it — this is what lets
                    # an admin know "you just re-pasted a question already asked N times,
                    # first on DATE" instead of it silently looking brand new.
                    cur.execute(
                        "SELECT times_shown, first_shown_at, last_shown_at FROM questions WHERE id = %s;",
                        (q["id"],)
                    )
                    prior = cur.fetchone()
                    if prior and (prior.get("times_shown") or 0) > 0:
                        repeat_alerts.append({
                            "id": q["id"],
                            "times_shown": prior.get("times_shown") or 0,
                            "first_shown_at": prior.get("first_shown_at"),
                            "last_shown_at": prior.get("last_shown_at"),
                        })

                    tags = q.get("tags")
                    if not isinstance(tags, list):
                        tags = [tags] if tags else []

                    options = q.get("options")
                    if not isinstance(options, list):
                        options = [options] if options else []

                    native_options = q.get("native_options")
                    if native_options is not None and not isinstance(native_options, list):
                        native_options = [native_options]

                    poll_explanation = Json(q.get("poll_explanation", {}))
                    options_analysis = Json(q.get("options_analysis", []))
                    scheduled_for = q.get("scheduled_for")
                    force_image = q.get("force_image", False)
                    native_question = q.get("native_question")

                    cur.execute("""
                        INSERT INTO questions (
                            id, subject, topic, difficulty, tags, question, latex, options,
                            correct_option, poll_explanation, options_analysis, scheduled_for, force_image,
                            native_question, native_options
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            subject = EXCLUDED.subject,
                            topic = EXCLUDED.topic,
                            difficulty = EXCLUDED.difficulty,
                            tags = EXCLUDED.tags,
                            question = EXCLUDED.question,
                            latex = EXCLUDED.latex,
                            options = EXCLUDED.options,
                            correct_option = EXCLUDED.correct_option,
                            poll_explanation = EXCLUDED.poll_explanation,
                            options_analysis = EXCLUDED.options_analysis,
                            scheduled_for = EXCLUDED.scheduled_for,
                            force_image = EXCLUDED.force_image,
                            native_question = EXCLUDED.native_question,
                            native_options = EXCLUDED.native_options;
                    """, (
                        q["id"], q["subject"], q["topic"], q.get("difficulty", "medium"),
                        tags, q["question"], q.get("latex"), options, int(q["correct_option"]),
                        poll_explanation, options_analysis, scheduled_for, force_image,
                        native_question, native_options
                    ))
                    imported_count += 1

                conn.commit()
                track_question_cache.invalidate_prefix("trackq:")
                self._last_import_repeats = repeat_alerts
                return imported_count
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to import questions: {e}{Style.RESET}")
            return 0
        finally:
            if conn:
                self.release_connection(conn)



    @staticmethod
    def load_json(path):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"{Style.RED}JSON Load Error ({path}): {e}{Style.RESET}")
        return {}

    @staticmethod
    def save_json(path, data):
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"{Style.RED}JSON Save Error ({path}): {e}{Style.RESET}")

    def refresh_database(self, force=False):
        now = time.time()
        if self.db and not force and (now - self.last_refresh < self.refresh_interval):
            return self.db

        self.db = {}
        self.last_refresh = now

        if self.db_url:
            conn = None
            try:
                conn = self.get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM questions;")
                    rows = cur.fetchall()

                    for row in rows:
                        q = dict(row)
                        for field in ["poll_explanation", "options_analysis"]:
                            if field in q and isinstance(q[field], str):
                                try:
                                    q[field] = json.loads(q[field])
                                except Exception:
                                    pass

                        subject = q.get("subject", "General").lower()
                        if subject not in self.db:
                            self.db[subject] = []
                        self.db[subject].append(q)
                    return self.db
            except Exception as e:
                print(f"{Style.YELLOW}[DB WARNING] Cloud loading failed, falling back to local files: {e}{Style.RESET}")
            finally:
                if conn:
                    self.release_connection(conn)

        return self.refresh_database_local()

    def refresh_database_local(self):
        self.db = {}
        questions_dir = Path("questions")
        if not questions_dir.exists():
            questions_dir.mkdir(exist_ok=True)

        for file_path in questions_dir.rglob("*.json"):
            data = self.load_json(str(file_path))
            questions_list = data if isinstance(data, list) else [data]
            for q in questions_list:
                if not q.get("id"):
                    continue
                subject = q.get("subject", "General").lower()
                if subject not in self.db:
                    self.db[subject] = []
                self.db[subject].append(q)
        return self.db

GLOBAL_ENGINE = QuizEngine()

def db_set_user_alliance(user_id, alliance_tag: str):
    clean_tag = alliance_tag.strip().replace("#", "").upper()
    clean_tag = "".join(c for c in clean_tag if c.isalnum() or c == "_")
    if not clean_tag:
        return False

    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, alliance_tag, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET alliance_tag = EXCLUDED.alliance_tag;
            """, (str(user_id), clean_tag))
            conn.commit()
            return clean_tag
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set user alliance: {e}")
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_alliance_leaderboard():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.org_id, o.org_tag AS alliance_tag, o.org_name, SUM(u.total_marks) as total_score, COUNT(m.user_id) as active_members
                FROM organizations o
                JOIN org_memberships m ON o.org_id = m.org_id
                JOIN user_stats u ON m.user_id = u.user_id
                WHERE m.org_role != 'pending'
                GROUP BY o.org_id, o.org_tag, o.org_name
                ORDER BY total_score DESC
                LIMIT 10;
            """)
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch alliance leaderboard: {e}")
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_responses_for_message(message_id: str, display_id: int = None):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT * FROM user_responses ORDER BY answered_at DESC LIMIT 5;")
                recent = cur.fetchall()
                print(f"[DEBUG-DB-DIAGNOSTIC] Last 5 rows in user_responses table:", flush=True)
                for r in recent:
                    print(f"  ├─ user_id={r['user_id']} | message_id={r['message_id']} | q_id={r['q_id']} | answered_at={r['answered_at']}", flush=True)
            except Exception as diag_err:
                print(f"[DEBUG-DB-DIAGNOSTIC-ERROR] Failed to run diagnostic: {diag_err}", flush=True)

            if display_id is not None:
                placeholder_id = f"launching_{display_id}"
                print(f"[DEBUG-DB-GET-RESPONSES] Querying user_responses for message_id={message_id} OR placeholder_id={placeholder_id}", flush=True)
                cur.execute("""
                    SELECT ur.user_id, ur.private_message_id, ur.selected_option, ur.is_correct, ur.answered_at,
                           us.nickname, us.username, us.first_name, us.public_consent_granted,
                           (SELECT o.org_tag FROM organizations o JOIN org_memberships m ON o.org_id = m.org_id WHERE m.user_id = ur.user_id LIMIT 1) AS alliance_tag,
                           (SELECT m.org_role FROM org_memberships m WHERE m.user_id = ur.user_id LIMIT 1) AS org_role
                    FROM user_responses ur
                    LEFT JOIN user_stats us ON ur.user_id = us.user_id
                    WHERE ur.message_id = %s OR ur.message_id = %s
                    ORDER BY ur.answered_at ASC;
                """, (str(message_id), placeholder_id))
            else:
                print(f"[DEBUG-DB-GET-RESPONSES] Querying user_responses for message_id={message_id}", flush=True)
                cur.execute("""
                    SELECT ur.user_id, ur.private_message_id, ur.selected_option, ur.is_correct, ur.answered_at,
                           us.nickname, us.username, us.first_name, us.public_consent_granted,
                           (SELECT o.org_tag FROM organizations o JOIN org_memberships m ON o.org_id = m.org_id WHERE m.user_id = ur.user_id LIMIT 1) AS alliance_tag,
                           (SELECT m.org_role FROM org_memberships m WHERE m.user_id = ur.user_id LIMIT 1) AS org_role
                    FROM user_responses ur
                    LEFT JOIN user_stats us ON ur.user_id = us.user_id
                    WHERE ur.message_id = %s
                    ORDER BY ur.answered_at ASC;
                """, (str(message_id),))
            rows = cur.fetchall()
            print(f"[DEBUG-DB-GET-RESPONSES] Query returned {len(rows)} rows.", flush=True)
            return rows
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch responses for message {message_id}: {e}")
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_overdue_tournament_rounds():
    from src.debug_log import dlog, dlog_exception
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *, EXTRACT(EPOCH FROM round_deadline) AS deadline_epoch
                FROM sent_tracks
                WHERE status = 'tournament_active';
            """)
            rows = [dict(r) for r in cur.fetchall()]

        dlog(f"[DEBUG-OVERDUE-SCAN] Query returned {len(rows)} row(s) with status='tournament_active'.")

        overdue = []
        now_epoch = GLOBAL_ENGINE.db_get_current_epoch()
        for r in rows:
            deadline_epoch = r.get('deadline_epoch')
            display_id = r.get('display_id')
            message_id = r.get('message_id')

            if deadline_epoch is None:
                dlog(f"[DEBUG-OVERDUE] REF {display_id} (mid={message_id}) | "
                     f"round_deadline is NULL in DB -> treating as OVERDUE immediately.")
                overdue.append(r)
                continue
            try:
                deadline_epoch = float(deadline_epoch)
            except (ValueError, TypeError) as cast_err:
                dlog(f"[DEBUG-OVERDUE] REF {display_id} (mid={message_id}) | "
                     f"deadline_epoch '{r.get('deadline_epoch')}' could not be cast to float "
                     f"({cast_err}) -> treating as OVERDUE immediately.")
                overdue.append(r)
                continue

            is_overdue = deadline_epoch <= now_epoch
            print(f"[DEBUG-OVERDUE] REF {display_id} | deadline={deadline_epoch:.1f} | "
                  f"now={now_epoch:.1f} | diff={deadline_epoch - now_epoch:.1f}s | "
                  f"is_overdue={is_overdue}", flush=True)

            if is_overdue:
                overdue.append(r)

        if overdue:
            dlog(f"[DEBUG-OVERDUE-SCAN] {len(overdue)} round(s) flagged overdue this tick: "
                 f"{[o.get('display_id') for o in overdue]}")

        return overdue
    except Exception as e:
        dlog_exception("db_get_overdue_tournament_rounds", e)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_active_tournament_rounds():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *, EXTRACT(EPOCH FROM round_deadline) AS deadline_epoch
                FROM sent_tracks
                WHERE status = 'tournament_active';
            """)
            rows = [dict(r) for r in cur.fetchall()]

            now_epoch = GLOBAL_ENGINE.db_get_current_epoch()
            active = []
            for r in rows:
                deadline_epoch = r.get('deadline_epoch')
                if deadline_epoch is not None:
                    try:
                        deadline_epoch = float(deadline_epoch)
                        r['remaining_seconds'] = max(0, int(deadline_epoch - now_epoch))
                    except (ValueError, TypeError):
                        r['remaining_seconds'] = 0
                else:
                    r['remaining_seconds'] = 0
                active.append(r)
            return active
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch active tournament rounds: {e}")
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_save_tournament_queue(remaining_ids: list, last_seq: int, round_seconds: int = 60, total_count: int = 1, scheduled_start=None, announcement_mid=None, cooldown_seconds: int = 15, tournament_meta: dict = None):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            if isinstance(remaining_ids, str):
                try:
                    remaining_ids = json.loads(remaining_ids)
                except Exception:
                    remaining_ids = []

            print(f"[DEBUG-DB-SAVE-QUEUE] Saving remaining_ids: {remaining_ids} (total={total_count}, display_id_offset={last_seq}) to database.", flush=True)

            meta_json = Json(tournament_meta) if tournament_meta is not None else '{}'

            cur.execute("""
                INSERT INTO tournament_queue (id, remaining_ids, last_seq, round_seconds, total_count, scheduled_start, announcement_mid, cooldown_seconds, tournament_meta, is_paused)
                VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                ON CONFLICT (id) DO UPDATE SET
                    remaining_ids = EXCLUDED.remaining_ids,
                    last_seq = EXCLUDED.last_seq,
                    round_seconds = EXCLUDED.round_seconds,
                    total_count = EXCLUDED.total_count,
                    scheduled_start = EXCLUDED.scheduled_start,
                    announcement_mid = EXCLUDED.announcement_mid,
                    cooldown_seconds = EXCLUDED.cooldown_seconds,
                    tournament_meta = EXCLUDED.tournament_meta,
                    is_paused = FALSE;
            """, (Json(remaining_ids), last_seq, round_seconds, total_count, scheduled_start, announcement_mid, cooldown_seconds, meta_json))
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to save tournament queue: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_tournament_queue():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tournament_queue WHERE id = 1;")
            row = cur.fetchone()
            if not row:
                return None
            res = dict(row)

            if isinstance(res.get('remaining_ids'), str):
                try:
                    res['remaining_ids'] = json.loads(res['remaining_ids'])
                except Exception:
                    res['remaining_ids'] = []

            print(f"[DEBUG-DB-GET-QUEUE] Retrieved row from DB: {res}", flush=True)
            return res
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch tournament queue: {e}")
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_peek_tournament_question():
    """Read-only peek at the next question in the queue — does NOT mutate the queue.
    The id is only removed via db_advance_tournament_queue() AFTER the round has
    actually launched successfully. This closes the crash window where a question
    could be popped from the DB but the process dies/restarts before the message
    is actually posted, silently losing that round forever."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT remaining_ids, last_seq FROM tournament_queue WHERE id = 1;")
            row = cur.fetchone()
            if not row:
                return None, None
            remaining = row['remaining_ids']
            if isinstance(remaining, str):
                try:
                    remaining = json.loads(remaining)
                except Exception:
                    remaining = []
            if not remaining:
                return None, None
            print(f"[DEBUG-DB-PEEK] Peeked next tournament question: '{remaining[0]}' (queue unchanged: {remaining})", flush=True)
            return remaining[0], row['last_seq'] + 1
    except Exception as e:
        print(f"[DB ERROR] Failed to peek tournament question: {e}")
        return None, None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_advance_tournament_queue(consumed_id, new_last_seq):
    """Commits the queue's progress AFTER a round has been CONFIRMED launched
    (message actually posted to Telegram). Idempotent: only removes consumed_id
    if it's still at the front of the list, so a duplicate call (e.g. from a
    race between two processes) is a safe no-op."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT remaining_ids, last_seq FROM tournament_queue WHERE id = 1 FOR UPDATE;")
            row = cur.fetchone()
            if not row:
                conn.commit()
                print(f"[DEBUG-DB-ADVANCE] No queue row found — nothing to advance for '{consumed_id}'.", flush=True)
                return False
            remaining = row['remaining_ids']
            if isinstance(remaining, str):
                try:
                    remaining = json.loads(remaining)
                except Exception:
                    remaining = []
            if remaining and remaining[0] == consumed_id:
                remaining.pop(0)
                cur.execute(
                    "UPDATE tournament_queue SET remaining_ids = %s, last_seq = %s WHERE id = 1;",
                    (Json(remaining), max(row['last_seq'], new_last_seq))
                )
                conn.commit()
                print(f"[DEBUG-DB-ADVANCE] Confirmed launch — removed '{consumed_id}' from queue. New remaining: {remaining}", flush=True)
                return True
            conn.commit()
            print(f"[DEBUG-DB-ADVANCE] '{consumed_id}' no longer at front of queue (front={remaining[0] if remaining else None}) — skipping, already advanced elsewhere.", flush=True)
            return False
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to advance tournament queue: {e}")
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_clear_tournament_queue():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tournament_queue WHERE id = 1;")
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to clear tournament queue: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_reset_tournament_scores(run_id: str = None):
    """
    Called immediately when a new tournament is SCHEDULED (not when Round 1 starts).
    Clears the tournament_run_id association from any previous tournament's tracks
    so the new run_id starts with a clean slate. The user_responses rows are kept
    (they count toward lifetime scores) — only the run_id tag that groups them into
    a tournament leaderboard is cleared.
    """
    if not run_id:
        return
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            # Mark any lingering 'tournament_active' tracks from a crashed prior run
            cur.execute("""
                UPDATE sent_tracks
                SET tournament_run_id = NULL
                WHERE tournament_run_id != %s
                  AND status IN ('tournament_active', 'closed', 'tournament_closed');
            """, (run_id,))
            conn.commit()
        print(f"[DB] Tournament scores reset for new run_id={run_id}", flush=True)
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[DB ERROR] db_reset_tournament_scores: {e}", flush=True)
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_question_by_id(q_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM questions WHERE id = %s;", (q_id,))
            row = cur.fetchone()
            if not row:
                return None
            q = dict(row)
            for field in ["poll_explanation", "options_analysis"]:
                if field in q and isinstance(q[field], str):
                    try:
                        q[field] = json.loads(q[field])
                    except Exception:
                        pass
            return q
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch question by id: {e}")
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_scheduling_pool(cooldown_days: int = 21, subject: str = None):
    """Returns candidate questions that haven't been shown within cooldown_days.
    This is the hard filter — cooldown-violating questions never even reach the scorer."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            params = [cooldown_days]
            clause = "WHERE (last_shown_at IS NULL OR last_shown_at < NOW() - (%s || ' days')::interval)"
            if subject:
                clause += " AND lower(subject) = lower(%s)"
                params.append(subject)
            cur.execute(f"""
                SELECT id, subject, topic, difficulty, tags, question, last_shown_at, times_shown
                FROM questions
                {clause}
                ORDER BY COALESCE(times_shown, 0) ASC;
            """, tuple(params))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch scheduling pool: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_recent_post_history(days: int = 7):
    """Feeds the diversity/balance scoring — what's actually gone out recently."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT q.subject, q.difficulty, q.topic, st.sent_at
                FROM sent_tracks st
                JOIN questions q ON st.q_id = q.id
                WHERE st.sent_at >= NOW() - (%s || ' days')::interval
                ORDER BY st.sent_at DESC;
            """, (days,))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch recent post history: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_mark_question_shown(q_id: str):
    """Call this every time a question is actually sent — scheduled, manual, or tournament.
    Sets first_shown_at only once (COALESCE keeps the original date forever), and always
    bumps last_shown_at + times_shown. This is what powers the 'repeat question' badge and
    the admin re-import warning."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE questions
                SET first_shown_at = COALESCE(first_shown_at, NOW()),
                    last_shown_at = NOW(),
                    times_shown = COALESCE(times_shown, 0) + 1
                WHERE id = %s;
            """, (q_id,))
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to mark question shown: {e}", flush=True)
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)
            
def db_get_question_history(q_id: str):
    """Single-question repeat-history lookup for admins: how many times it's gone out,
    and its first/last send dates. Returns None if the question has never been sent."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, subject, topic, times_shown, first_shown_at, last_shown_at
                FROM questions WHERE id = %s;
            """, (q_id,))
            row = cur.fetchone()
            if not row or not (row.get("times_shown") or 0):
                return None
            return dict(row)
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch question history for {q_id}: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_track_and_question(display_id: int):
    cache_key = f"trackq:{display_id}"
    cached = track_question_cache.get(cache_key)
    if cached is not None:
        track, row_dict = cached
        return dict(track), dict(row_dict)

    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    t.message_id AS track_message_id,
                    t.status AS track_status,
                    t.display_id AS track_display_id,
                    t.type AS track_type,
                    t.msg_type AS track_msg_type,
                    t.followup_mid AS track_followup_mid,
                    q.*
                FROM sent_tracks t
                JOIN questions q ON t.q_id = q.id
                WHERE t.display_id = %s;
            """, (int(display_id),))
            row = cur.fetchone()
            if not row:
                return None, None

            row_dict = dict(row)
            track = {
                "message_id": row_dict.pop("track_message_id"),
                "status": row_dict.pop("track_status"),
                "display_id": row_dict.pop("track_display_id"),
                "type": row_dict.pop("track_type"),
                "msg_type": row_dict.pop("track_msg_type"),
                "followup_mid": row_dict.pop("track_followup_mid"),
                "q_id": row_dict["id"]
            }

            for field in ["poll_explanation", "options_analysis"]:
                if field in row_dict and isinstance(row_dict[field], str):
                    try:
                        row_dict[field] = json.loads(row_dict[field])
                    except Exception:
                        pass

            track_question_cache.set(cache_key, (dict(track), dict(row_dict)))
            return track, row_dict
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch track and question: {e}")
        return None, None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_cached_file_id(cache_key: str):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT file_id FROM compiled_assets_cache WHERE cache_key = %s;", (cache_key,))
            row = cur.fetchone()
            return row['file_id'] if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch cached file_id: {e}")
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_save_cached_file_id(cache_key: str, file_id: str):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO compiled_assets_cache (cache_key, file_id)
                VALUES (%s, %s)
                ON CONFLICT (cache_key) DO UPDATE SET file_id = EXCLUDED.file_id;
            """, (cache_key, file_id))
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to save file_id cache: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_set_user_grade(user_id, grade: int):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, grade, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET grade = EXCLUDED.grade;
            """, (str(user_id), int(grade)))
            user_profile_cache.invalidate(f"profile:{user_id}")
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set user grade: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_profile(user_id):
    cache_key = f"profile:{user_id}"
    cached = user_profile_cache.get(cache_key)
    if cached is not None:
        return dict(cached)
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.*,
                       (SELECT o.org_name FROM organizations o JOIN org_memberships m ON o.org_id = m.org_id WHERE m.user_id = u.user_id LIMIT 1) AS org_name,
                       (SELECT o.org_tag FROM organizations o JOIN org_memberships m ON o.org_id = m.org_id WHERE m.user_id = u.user_id LIMIT 1) AS org_tag,
                       (SELECT m.org_role FROM org_memberships m WHERE m.user_id = u.user_id LIMIT 1) AS org_role,
                       (SELECT o.org_id FROM organizations o JOIN org_memberships m ON o.org_id = m.org_id WHERE m.user_id = u.user_id LIMIT 1) AS org_id
                FROM user_stats u
                WHERE u.user_id = %s;
            """, (str(user_id),))
            row = cur.fetchone()
            result = dict(row) if row else None
            if result:
                user_profile_cache.set(cache_key, result)
            return result
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user profile: {e}")
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_subject_marks(user_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT subject, marks FROM user_subject_marks
                WHERE user_id = %s ORDER BY marks DESC;
            """, (str(user_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch subject marks: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_response(user_id, message_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM user_responses
                WHERE user_id = %s AND message_id = %s;
            """, (str(user_id), str(message_id)))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user response: {e}")
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_update_private_message_id(user_id, message_id, private_message_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_responses
                SET private_message_id = %s
                WHERE user_id = %s AND message_id = %s;
            """, (int(private_message_id), str(user_id), str(message_id)))
            conn.commit()
            print(f"[DEBUG-DB-PM-ID] Modified private_message_id={private_message_id} for user_id={user_id}, message_id={message_id}. Rowcount: {cur.rowcount}", flush=True)
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update private message ID: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_update_response_view_state(user_id, message_id, show_derivation: bool, show_perf: bool):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE user_responses
                SET show_derivation = %s, show_perf = %s
                WHERE user_id = %s AND message_id = %s;
            """, (show_derivation, show_perf, str(user_id), str(message_id)))
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update response view state: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_weekly_leaderboard(grade: int):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ur.user_id, SUM(ur.marks_awarded) as total_score,
                       us.nickname, us.username, us.first_name, us.public_consent_granted,
                       (SELECT o.org_tag FROM organizations o JOIN org_memberships m ON o.org_id = m.org_id WHERE m.user_id = ur.user_id LIMIT 1) AS alliance_tag
                FROM user_responses ur
                JOIN user_stats us ON ur.user_id = us.user_id
                WHERE us.grade = %s
                  AND ur.answered_at >= NOW() - INTERVAL '7 days'
                GROUP BY ur.user_id, us.nickname, us.username, us.first_name, us.public_consent_granted
                ORDER BY total_score DESC
                LIMIT 10;
            """, (int(grade),))
            rows = cur.fetchall()
            return rows
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch weekly leaderboard: {e}")
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_pending_scheduled_question():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM questions
                WHERE is_sent = FALSE
                  AND scheduled_for IS NOT NULL
                  AND scheduled_for <= NOW()
                ORDER BY scheduled_for ASC
                LIMIT 1;
            """)
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch scheduled question: {e}")
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_mark_question_as_sent(q_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE questions SET is_sent = TRUE WHERE id = %s;", (q_id,))
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to mark question as sent: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def process_user_score(user_id, message_id, q_id, is_correct, selected_option, private_message_id=None, show_derivation=False, show_perf=False):
    from src.debug_log import dlog, dlog_exception
    max_attempts = 2
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        conn = None
        try:
            dlog(f"[DEBUG-DB-SCORE] Attempt {attempt}/{max_attempts} | user={user_id}, "
                 f"message_id={message_id}, q_id={q_id}, correct={is_correct}, "
                 f"selected_option={selected_option}")
            conn = GLOBAL_ENGINE.get_db_connection()
            with conn.cursor() as cur:
                pm_id = int(private_message_id) if private_message_id is not None else None
                cur.execute(
                    """
                    SELECT * FROM fn_process_user_score(
                        %s::text, %s::text, %s::text, %s::boolean, %s::integer,
                        %s::bigint, %s::boolean, %s::boolean
                    );
                    """,
                    (
                        str(user_id), str(message_id), q_id, bool(is_correct), int(selected_option),
                        pm_id, bool(show_derivation), bool(show_perf)
                    )
                )
                row = cur.fetchone()
                conn.commit()
                dlog(f"[DEBUG-DB-SCORE] Attempt {attempt} succeeded for user={user_id}, "
                     f"message_id={message_id}. Row output: {dict(row) if row else 'None'}")

            if not row:
                dlog(f"[DEBUG-DB-SCORE] fn_process_user_score returned NO ROW for "
                     f"user={user_id}, message_id={message_id}. Returning None.")
                return None

            total = row['o_total']
            correct = row['o_correct']
            accuracy = int((correct / total) * 100) if total and total > 0 else 0
            return {
                "total": total,
                "correct": correct,
                "accuracy": accuracy,
                "total_marks": row['o_total_marks'],
                "marks_awarded": row['o_marks_awarded'],
                "first_try": row['o_first_try'],
                "speed_tier": row['o_speed_tier'],
                "grade": row['o_grade'],
                "current_streak": row['o_current_streak'],
            }
        except Exception as e:
            last_exc = e
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            dlog_exception(
                f"process_user_score (attempt {attempt}/{max_attempts}, "
                f"user={user_id}, message_id={message_id}, q_id={q_id})", e
            )
            if attempt < max_attempts:
                time.sleep(0.5)
                continue
        finally:
            if conn:
                GLOBAL_ENGINE.release_connection(conn)

    dlog(f"[DEBUG-DB-SCORE] All {max_attempts} attempts failed for user={user_id}, "
         f"message_id={message_id}. Raising last exception to caller.")
    raise last_exc

def db_try_start_tournament_round(message_id, q_id, display_id, round_seconds, round_number, total_rounds, tournament_run_id=None):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(872364193);")
            cur.execute("SELECT 1 FROM sent_tracks WHERE status = 'tournament_active' LIMIT 1;")
            if cur.fetchone():
                conn.rollback()
                return False
            cur.execute("""
                INSERT INTO sent_tracks
                    (message_id, q_id, status, display_id, type, msg_type, round_deadline, round_number, total_rounds, tournament_run_id)
                VALUES
                    (%s, %s, 'tournament_active', %s, 'premium', 'text',
                     NOW() + (%s || ' second')::interval, %s, %s, %s)
                ON CONFLICT (message_id) DO NOTHING;
            """, (str(message_id), q_id, int(display_id), int(round_seconds), int(round_number), int(total_rounds), tournament_run_id))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to claim tournament round: {e}")
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_set_tournament_pause_state(paused: bool):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tournament_queue
                SET is_paused = %s
                WHERE id = 1;
            """, (paused,))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update tournament pause state: {e}")
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_upcoming_scheduled_questions():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, subject, topic, question, scheduled_for, difficulty
                FROM questions
                WHERE is_sent = FALSE
                  AND scheduled_for IS NOT NULL
                ORDER BY scheduled_for ASC;
            """)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] Failed to get upcoming scheduled questions: {e}")
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_reschedule_question(q_id: str, new_time_str: str or None):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE questions
                SET scheduled_for = %s
                WHERE id = %s;
            """, (new_time_str, q_id))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to reschedule question {q_id}: {e}")
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_update_tournament_schedule_params(scheduled_start=None, round_seconds=None, cooldown_seconds=None):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            updates = []
            params = []
            if scheduled_start is not None:
                updates.append("scheduled_start = %s")
                params.append(scheduled_start if scheduled_start != "CLEAR" else None)
            if round_seconds is not None:
                updates.append("round_seconds = %s")
                params.append(int(round_seconds))
            if cooldown_seconds is not None:
                updates.append("cooldown_seconds = %s")
                params.append(int(cooldown_seconds))

            if not updates:
                return False

            params.append(1)
            cur.execute(f"""
                UPDATE tournament_queue
                SET {", ".join(updates)}
                WHERE id = %s;
            """, tuple(params))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update tournament schedule parameters: {e}")
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_update_user_telegram_info(user_id, username, first_name):
    """Upserts the user's latest real Telegram handle and first name."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, username, first_name, total, correct, total_marks)
                VALUES (%s, %s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name;
            """, (str(user_id), username, first_name))
            conn.commit()
            print(f"[DEBUG-DB-USER-SYNC] Synced profile for {user_id} -> Username: {username}, Name: {first_name}", flush=True)
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to sync user telegram attributes: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_set_user_nickname(user_id, nickname):
    """Sets or clears a custom, student-defined display nickname."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, nickname, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET
                    nickname = EXCLUDED.nickname;
            """, (str(user_id), nickname))
            user_profile_cache.invalidate(f"profile:{user_id}")
            conn.commit()
            print(f"[DEBUG-DB-NICKNAME] User {user_id} configured custom nickname to: '{nickname}'", flush=True)
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to store user custom nickname: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_update_tournament_meta_field(key: str, value):
    """Safely updates a specified key within the JSONB tournament_meta column inside the tournament_queue table."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE tournament_queue
                SET tournament_meta = COALESCE(tournament_meta, '{}'::jsonb) || %s::jsonb
                WHERE id = 1;
            """, (Json({key: value}),))
            conn.commit()
            print(f"[CONSOLIDATED-FIX] Successfully updated JSONB tournament_meta field: {key} -> {value}", flush=True)
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update tournament_meta field {key}: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_create_organization(org_name: str, org_tag: str, creator_id: str, org_type: str = "School", is_public: bool = True, city: str = "Addis Ababa", country: str = "Ethiopia", status: str = "approved") -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO organizations (org_name, org_tag, creator_id, org_type, is_public, city, country, join_token, status)
                VALUES (%s, UPPER(%s), %s, %s, %s, %s, %s, %s, %s)
                RETURNING org_id;
            """, (org_name, org_tag, str(creator_id), org_type, is_public, city, country, secrets.token_hex(16), status))
            org_id = cur.fetchone()['org_id']

            cur.execute("""
                INSERT INTO org_memberships (user_id, org_id, org_role)
                VALUES (%s, %s, 'creator')
                ON CONFLICT (user_id, org_id) DO UPDATE SET org_role = EXCLUDED.org_role;
            """, (str(creator_id), org_id))

            cur.execute("UPDATE user_stats SET timezone = %s WHERE user_id = %s;", (get_timezone_for_country(country), str(creator_id)))

            conn.commit()
            return org_id
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Create organization failed: {e}", flush=True)
        raise e
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_join_organization(user_id, org_tag: str) -> dict:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT org_id, org_name, is_public, creator_id, country, team_scope, scope_value FROM organizations WHERE org_tag = UPPER(%s) AND deleted_at IS NULL;", (org_tag.strip(),))
            row = cur.fetchone()
            if not row:
                return None
            org_id, org_name, is_public, creator_id, country = row['org_id'], row['org_name'], row['is_public'], row['creator_id'], row['country']

            if row.get('team_scope') and row['team_scope'] != 'open':
                elig = db_check_team_scope_eligibility(user_id, org_id)
                if not elig["eligible"]:
                    return {"scope_blocked": True, "reason": elig["reason"], "org_name": org_name}

            cur.execute("SELECT org_role, request_count, last_requested_at FROM org_memberships WHERE user_id = %s AND org_id = %s;", (str(user_id), org_id))
            existing = cur.fetchone()
            if existing and existing['org_role'] in ('creator', 'admin', 'member'):
                return {"org_id": org_id, "org_name": org_name, "role_assigned": existing['org_role'], "creator_id": creator_id, "already_member": True}
            if existing and existing['org_role'] == 'pending':
                return {
                    "org_id": org_id, "org_name": org_name, "role_assigned": "pending", "creator_id": creator_id,
                    "already_pending": True, "request_count": existing['request_count'], "last_requested_at": existing['last_requested_at']
                }

            role = "pending" if is_public else "member"
            cur.execute("""
                INSERT INTO org_memberships (user_id, org_id, org_role, request_count, last_requested_at)
                VALUES (%s, %s, %s, 1, NOW())
                ON CONFLICT (user_id, org_id) DO UPDATE SET
                    org_role = EXCLUDED.org_role, joined_at = NOW(), request_count = 1, last_requested_at = NOW()
                WHERE org_memberships.org_role = 'rejected';
            """, (str(user_id), org_id, role))
            cur.execute("UPDATE user_stats SET timezone = %s WHERE user_id = %s;", (get_timezone_for_country(country), str(user_id)))
            conn.commit()
            return {"org_id": org_id, "org_name": org_name, "role_assigned": role, "creator_id": creator_id}
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Join failed: {e}", flush=True)
        raise e
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_resend_join_request(user_id, org_id: int) -> bool:
    """Bumps request_count + last_requested_at for an already-pending request."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE org_memberships SET request_count = COALESCE(request_count, 1) + 1, last_requested_at = NOW()
                WHERE user_id = %s AND org_id = %s AND org_role = 'pending';
            """, (str(user_id), int(org_id)))
            conn.commit()
            return cur.rowcount > 0
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Resend request failed: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_join_organization_by_id(user_id, org_id: int) -> dict:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT org_id, org_name, is_public, creator_id, country, team_scope, scope_value FROM organizations WHERE org_id = %s AND deleted_at IS NULL;", (int(org_id),))
            row = cur.fetchone()
            if not row:
                return None

            if row.get('team_scope') and row['team_scope'] != 'open':
                elig = db_check_team_scope_eligibility(user_id, int(org_id))
                if not elig["eligible"]:
                    return {"scope_blocked": True, "reason": elig["reason"], "org_name": row['org_name']}

            cur.execute("SELECT org_role, request_count, last_requested_at FROM org_memberships WHERE user_id = %s AND org_id = %s;", (str(user_id), int(org_id)))
            existing = cur.fetchone()
            if existing and existing['org_role'] in ('creator', 'admin', 'member'):
                return {"org_id": row['org_id'], "org_name": row['org_name'], "role_assigned": existing['org_role'], "creator_id": row['creator_id'], "already_member": True}
            if existing and existing['org_role'] == 'pending':
                return {
                    "org_id": row['org_id'], "org_name": row['org_name'], "role_assigned": "pending", "creator_id": row['creator_id'],
                    "already_pending": True, "request_count": existing['request_count'], "last_requested_at": existing['last_requested_at']
                }

            role = "pending" if row['is_public'] else "member"
            cur.execute("""
                INSERT INTO org_memberships (user_id, org_id, org_role)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, org_id) DO UPDATE SET
                    org_role = EXCLUDED.org_role, joined_at = NOW()
                WHERE org_memberships.org_role = 'rejected';
            """, (str(user_id), int(org_id), role))
            cur.execute("UPDATE user_stats SET timezone = %s WHERE user_id = %s;", (get_timezone_for_country(row['country']), str(user_id)))
            conn.commit()
            return {"org_id": row['org_id'], "org_name": row['org_name'], "role_assigned": role, "creator_id": row['creator_id']}
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Join by id failed: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_check_team_scope_eligibility(user_id, org_id: int) -> dict:
    """Returns {'eligible': bool, 'reason': str|None, 'scope': str, 'scope_value': str|None}.
    A dedicated team only accepts joiners whose OWN profile (personal or org-derived) matches
    the scope it was created for. Open teams always pass."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT team_scope, scope_value, city, country FROM organizations WHERE org_id = %s;", (int(org_id),))
            org = cur.fetchone()
            if not org or org['team_scope'] == 'open':
                return {"eligible": True, "reason": None, "scope": org['team_scope'] if org else 'open', "scope_value": None}

            cur.execute("""
                SELECT COALESCE(o.country, u.personal_country) AS country,
                       COALESCE(o.city, u.personal_city) AS city
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE u.user_id = %s
                LIMIT 1;
            """, (str(user_id),))
            joiner = cur.fetchone() or {}

            scope = org['team_scope']
            if scope == 'country':
                ok = joiner.get('country') == org['scope_value']
            elif scope == 'city':
                ok = joiner.get('city') == org['scope_value']
            elif scope == 'school':
                # School-dedicated just means: this IS the school team — joining it directly
                # is always allowed (that's the normal join path); scope restriction here
                # only matters for the "create your own dedicated team" flow, not joining.
                ok = True
            else:
                ok = True

            reason = None if ok else f"This team is only open to students in {org['scope_value']}."
            return {"eligible": ok, "reason": reason, "scope": scope, "scope_value": org['scope_value']}
    except Exception as e:
        print(f"[DB ERROR] db_check_team_scope_eligibility: {e}", flush=True)
        return {"eligible": True, "reason": None, "scope": "open", "scope_value": None}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_create_dedicated_organization(org_name: str, org_tag: str, creator_id: str, team_scope: str,
                                      scope_value: str, description: str, city: str, country: str) -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO organizations (org_name, org_tag, creator_id, org_type, is_public, city, country,
                                            join_token, team_scope, scope_value, description)
                VALUES (%s, UPPER(%s), %s, 'School', TRUE, %s, %s, %s, %s, %s, %s)
                RETURNING org_id;
            """, (org_name, org_tag, str(creator_id), city, country, secrets.token_hex(16), team_scope, scope_value, description))
            org_id = cur.fetchone()['org_id']
            cur.execute("""
                INSERT INTO org_memberships (user_id, org_id, org_role) VALUES (%s, %s, 'creator')
                ON CONFLICT (user_id, org_id) DO UPDATE SET org_role = EXCLUDED.org_role;
            """, (str(creator_id), org_id))
            conn.commit()
            return org_id
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] db_create_dedicated_organization: {e}", flush=True)
        raise e
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)
            

def db_join_organization_by_token(user_id, join_token: str) -> dict:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT org_id, org_name, is_public, creator_id, country FROM organizations WHERE join_token = %s;", (join_token,))
            row = cur.fetchone()
            if not row:
                return None

            cur.execute("SELECT org_role FROM org_memberships WHERE user_id = %s AND org_id = %s;", (str(user_id), row['org_id']))
            existing = cur.fetchone()
            if existing and existing['org_role'] in ('creator', 'admin', 'member'):
                return {"org_id": row["org_id"], "org_name": row["org_name"], "role_assigned": existing['org_role'], "creator_id": row["creator_id"], "already_member": True}
            if existing and existing['org_role'] == 'pending':
                return {"org_id": row["org_id"], "org_name": row["org_name"], "role_assigned": "pending", "creator_id": row["creator_id"], "already_pending": True}

            role = "pending" if row["is_public"] else "member"
            cur.execute("""
                INSERT INTO org_memberships (user_id, org_id, org_role)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id, org_id) DO UPDATE SET
                    org_role = EXCLUDED.org_role, joined_at = NOW()
                WHERE org_memberships.org_role = 'rejected';
            """, (str(user_id), row["org_id"], role))
            cur.execute("UPDATE user_stats SET timezone = %s WHERE user_id = %s;", (get_timezone_for_country(row['country']), str(user_id)))
            conn.commit()
            return {"org_id": row["org_id"], "org_name": row["org_name"], "role_assigned": role, "creator_id": row["creator_id"]}
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Join by token failed: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_leave_organization(user_id, org_id: int):
    """Removes a member. If the creator leaves, the longest-standing admin (or, if none,
    the longest-standing member) is automatically promoted — a team can never be left
    without an owner."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT org_role FROM org_memberships WHERE user_id = %s AND org_id = %s;", (str(user_id), int(org_id)))
            row = cur.fetchone()
            was_creator = bool(row and row['org_role'] == 'creator')

            cur.execute(
                "UPDATE org_memberships SET org_role = 'left', deleted_at = NOW() WHERE user_id = %s AND org_id = %s;",
                (str(user_id), int(org_id))
            )

            promoted_id = None
            if was_creator:
                cur.execute("SELECT user_id FROM org_memberships WHERE org_id = %s AND org_role = 'admin' ORDER BY joined_at ASC LIMIT 1;", (int(org_id),))
                nxt = cur.fetchone()
                if not nxt:
                    cur.execute("SELECT user_id FROM org_memberships WHERE org_id = %s AND org_role = 'member' ORDER BY joined_at ASC LIMIT 1;", (int(org_id),))
                    nxt = cur.fetchone()
                if nxt:
                    promoted_id = nxt['user_id']
                    cur.execute("UPDATE org_memberships SET org_role = 'creator' WHERE user_id = %s AND org_id = %s;", (promoted_id, int(org_id)))
                    cur.execute("UPDATE organizations SET creator_id = %s WHERE org_id = %s;", (promoted_id, int(org_id)))

            conn.commit()
            return {"was_creator": was_creator, "promoted_id": promoted_id}
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Leave failed: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_organizations(user_id):
    """Retrieves all school teams a student is actively mapped to."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.*, m.org_role
                FROM organizations o
                JOIN org_memberships m ON o.org_id = m.org_id
                WHERE m.user_id = %s AND o.deleted_at IS NULL AND m.org_role NOT IN ('rejected', 'left');
            """, (str(user_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB-ORG-ERROR] Fetch user orgs failed: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_count_referrals(referrer_id) -> int:
    """Counts how many users this person has referred — used to throttle notifications."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM user_stats WHERE referred_by = %s;", (str(referrer_id),))
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count referrals: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_find_similar_organizations(name: str):
    """Looks for existing teams with a similar name before letting someone create a duplicate."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            core = re.sub(r'[^\w\s]', '', name).strip()
            if not core:
                return []
            cur.execute("""
                SELECT org_id, org_name, org_tag, city, country
                FROM organizations
                WHERE org_name ILIKE %s AND deleted_at IS NULL
                LIMIT 5;
            """, (f"%{core}%",))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB-ORG-ERROR] Similar org search failed: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_organization_roster(org_id: int):
    """Active roster only (pending/rejected are admin-only, via db_get_org_membership_log).
    total_marks here is the member's CONTRIBUTION TO THIS TEAM specifically (from the ledger),
    not their lifetime score — a student on 2 teams shows a different number on each roster,
    split evenly between however many teams they're currently active on."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.user_id, u.nickname, u.username, u.first_name,
                       COALESCE(c.marks, 0)::int AS total_marks,
                       m.org_role, m.joined_at, u.public_consent_granted
                FROM org_memberships m
                JOIN user_stats u ON m.user_id = u.user_id
                LEFT JOIN user_org_contributions c ON c.user_id = m.user_id AND c.org_id = m.org_id
                WHERE m.org_id = %s AND m.org_role NOT IN ('pending', 'rejected', 'left')
                ORDER BY total_marks DESC;
            """, (int(org_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB-ORG-ERROR] Fetch roster failed: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_org_membership_log(org_id: int, limit: int = 40):
    """Full roster + request history: members, admins, pending, and rejected."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.user_id, u.nickname, u.username, u.first_name, u.public_consent_granted,
                       u.total_marks, m.org_role, m.joined_at
                FROM org_memberships m
                JOIN user_stats u ON m.user_id = u.user_id
                WHERE m.org_id = %s
                ORDER BY
                    CASE m.org_role
                        WHEN 'creator' THEN 0 WHEN 'admin' THEN 1 WHEN 'member' THEN 2
                        WHEN 'pending' THEN 3 ELSE 4
                    END,
                    m.joined_at DESC
                LIMIT %s;
            """, (int(org_id), limit))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB-ORG-ERROR] Fetch membership log failed: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_org_role(user_id, org_id: int):
    """Role check scoped to ONE specific org — the old code read role from the user's
    generic profile (which grabs an arbitrary org via LIMIT 1 if they're in multiple
    teams), so admins of team B could get denied access when it returned their role
    from team A instead. This queries the exact membership row."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT org_role FROM org_memberships WHERE user_id = %s AND org_id = %s;", (str(user_id), int(org_id)))
            row = cur.fetchone()
            return row['org_role'] if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user org role: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_subjects_summary(user_id):
    """Subject picker for /myanswers: total questions vs. how many this user answered."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT q.subject,
                       COUNT(DISTINCT q.id) AS total_count,
                       COUNT(DISTINCT ur.q_id) AS answered_count
                FROM questions q
                LEFT JOIN user_responses ur ON ur.q_id = q.id AND ur.user_id = %s
                GROUP BY q.subject
                ORDER BY q.subject ASC;
            """, (str(user_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user subjects summary: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_user_question_matrix(user_id, subject: str = None, filter_mode: str = "all", limit: int = 8, offset: int = 0, sort_field: str = "topic", sort_dir: str = "asc"):
    """Every question (optionally scoped to a subject) tagged with whether THIS user
    answered it, plus the date they first answered (if any). filter_mode: 'all' | 'answered' | 'unanswered'.
    sort_field: 'topic' | 'date' | 'tags' | 'difficulty'. sort_dir: 'asc' | 'desc'.
    Questions this user personally hid (user_hidden_questions) never appear here — this
    is a purely personal, non-destructive hide; the record and every other user's view
    are untouched."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            sort_columns = {"topic": "topic", "date": "answered_at", "tags": "tags_text", "difficulty": "difficulty"}
            sort_col = sort_columns.get(sort_field, "topic")
            direction = "DESC" if str(sort_dir).lower() == "desc" else "ASC"

            cur.execute(f"""
                WITH latest AS (
                    SELECT DISTINCT ON (q.id)
                        q.id AS q_id, q.subject, q.topic, q.difficulty, q.tags,
                        array_to_string(q.tags, ', ') AS tags_text,
                        q.question,
                        st.display_id, st.message_id, st.status AS track_status, st.sent_at,
                        ur.is_correct, ur.marks_awarded, ur.answered_at
                    FROM questions q
                    LEFT JOIN sent_tracks st ON st.q_id = q.id
                    LEFT JOIN user_responses ur ON ur.message_id = st.message_id AND ur.user_id = %s
                    WHERE (%s::text IS NULL OR lower(q.subject) = lower(%s))
                      AND NOT EXISTS (
                          SELECT 1 FROM user_hidden_questions h
                          WHERE h.user_id = %s AND h.q_id = q.id
                      )
                    ORDER BY q.id, st.sent_at DESC NULLS LAST
                )
                SELECT * FROM latest
                WHERE CASE
                        WHEN %s = 'answered' THEN is_correct IS NOT NULL
                        WHEN %s = 'unanswered' THEN is_correct IS NULL
                        ELSE TRUE
                      END
                ORDER BY subject ASC, {sort_col} {direction} NULLS LAST, q_id ASC
                LIMIT %s OFFSET %s;
            """, (str(user_id), subject, subject, str(user_id), filter_mode, filter_mode, limit, offset))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user question matrix: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_count_user_question_matrix(user_id, subject: str = None, filter_mode: str = "all") -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (q.id) q.id AS q_id, q.subject, ur.is_correct
                    FROM questions q
                    LEFT JOIN sent_tracks st ON st.q_id = q.id
                    LEFT JOIN user_responses ur ON ur.message_id = st.message_id AND ur.user_id = %s
                    WHERE (%s::text IS NULL OR lower(q.subject) = lower(%s))
                      AND NOT EXISTS (
                          SELECT 1 FROM user_hidden_questions h
                          WHERE h.user_id = %s AND h.q_id = q.id
                      )
                    ORDER BY q.id, st.sent_at DESC NULLS LAST
                )
                SELECT COUNT(*) AS cnt FROM latest
                WHERE CASE
                        WHEN %s = 'answered' THEN is_correct IS NOT NULL
                        WHEN %s = 'unanswered' THEN is_correct IS NULL
                        ELSE TRUE
                      END;
            """, (str(user_id), subject, subject, str(user_id), filter_mode, filter_mode))
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count user question matrix: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_latest_track_for_question(q_id: str):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM sent_tracks WHERE q_id = %s ORDER BY sent_at DESC NULLS LAST LIMIT 1;", (q_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch latest track for question {q_id}: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_admin_question_overview(subject: str = None, status_filter: str = "all", limit: int = 10, offset: int = 0):
    """Admin master list. status_filter: 'all' | 'posted' | 'unposted' | 'deleted'."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (q.id)
                        q.id AS q_id, q.subject, q.topic, q.difficulty, q.scheduled_for, q.is_sent,
                        q.times_shown, q.first_shown_at,
                        st.display_id, st.message_id, st.status AS track_status, st.sent_at
                    FROM questions q
                    LEFT JOIN sent_tracks st ON st.q_id = q.id
                    ORDER BY q.id, st.sent_at DESC NULLS LAST
                ),
                stats AS (
                    SELECT l.q_id,
                           COUNT(ur.user_id) AS answer_count,
                           COUNT(ur.user_id) FILTER (WHERE ur.is_correct) AS correct_count
                    FROM latest l
                    LEFT JOIN user_responses ur ON ur.message_id = l.message_id
                    GROUP BY l.q_id
                )
                SELECT l.*, COALESCE(s.answer_count, 0) AS answer_count, COALESCE(s.correct_count, 0) AS correct_count
                FROM latest l JOIN stats s ON s.q_id = l.q_id
                WHERE (%s::text IS NULL OR lower(l.subject) = lower(%s))
                  AND (
                        %s = 'all'
                        OR (%s = 'unposted' AND l.message_id IS NULL)
                        OR (%s = 'posted' AND l.message_id IS NOT NULL AND l.track_status != 'deleted')
                        OR (%s = 'deleted' AND l.track_status = 'deleted')
                      )
                ORDER BY l.subject ASC, l.q_id ASC
                LIMIT %s OFFSET %s;
            """, (subject, subject, status_filter, status_filter, status_filter, status_filter, limit, offset))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch admin question overview: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_count_admin_questions(subject: str = None, status_filter: str = "all") -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (q.id) q.id AS q_id, q.subject, st.message_id, st.status AS track_status
                    FROM questions q
                    LEFT JOIN sent_tracks st ON st.q_id = q.id
                    ORDER BY q.id, st.sent_at DESC NULLS LAST
                )
                SELECT COUNT(*) AS cnt FROM latest
                WHERE (%s::text IS NULL OR lower(subject) = lower(%s))
                  AND (
                        %s = 'all'
                        OR (%s = 'unposted' AND message_id IS NULL)
                        OR (%s = 'posted' AND message_id IS NOT NULL AND track_status != 'deleted')
                        OR (%s = 'deleted' AND track_status = 'deleted')
                      );
            """, (subject, subject, status_filter, status_filter, status_filter, status_filter))
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count admin questions: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_update_organization_profile(org_id: int, new_name: str = None, new_tag: str = None, is_public: bool = None) -> bool:
    """Updates metadata profile fields for an active organization."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            if new_name:
                cur.execute("UPDATE organizations SET org_name = %s WHERE org_id = %s;", (new_name, int(org_id)))
            if new_tag:
                cur.execute("UPDATE organizations SET org_tag = UPPER(%s) WHERE org_id = %s;", (new_tag.strip(), int(org_id)))
            if is_public is not None:
                cur.execute("UPDATE organizations SET is_public = %s WHERE org_id = %s;", (bool(is_public), int(org_id)))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Update profile failed: {e}", flush=True)
        raise e
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_dissolve_organization(org_id: int) -> bool:
    """Soft-deletes an organization — never hard-deletes. Memberships are left intact
    for historical/audit purposes; the org simply stops appearing in active queries."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE organizations SET deleted_at = NOW() WHERE org_id = %s;", (int(org_id),))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Soft-dissolution failed: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_update_user_consent_state(user_id, consent: bool) -> bool:
    """Commits user's chosen dynamic privacy opt-in consent state."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, public_consent_granted, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET
                    public_consent_granted = EXCLUDED.public_consent_granted;
            """, (str(user_id), bool(consent)))
            user_profile_cache.invalidate(f"profile:{user_id}")
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to store user consent update: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


# --- SECURITY & ACCESS PRIVILEGES CONTROLLER ---

def db_get_pending_org_requests(org_id: int):
    """Retrieves all pending student join requests for verification."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.user_id, u.nickname, u.username, u.first_name, u.total_marks
                FROM org_memberships m
                JOIN user_stats u ON m.user_id = u.user_id
                WHERE m.org_id = %s AND m.org_role = 'pending'
                ORDER BY m.joined_at ASC;
            """, (int(org_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB-ORG-ERROR] Fetch pending failed: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_approve_member_request(user_id, org_id: int, approve: bool) -> bool:
    """Approves or rejects a pending request. Rejections are KEPT as 'rejected'
    rows (not deleted) so the team's request history stays visible."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            new_role = "member" if approve else "rejected"
            cur.execute("""
                UPDATE org_memberships
                SET org_role = %s, joined_at = NOW()
                WHERE user_id = %s AND org_id = %s;
            """, (new_role, str(user_id), int(org_id)))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Approval processing failed: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_promote_member(user_id, org_id: int, promote: bool) -> bool:
    """Promotes a standard member to Administrator or demotes an Admin back to Member."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            role = "admin" if promote else "member"
            cur.execute("""
                UPDATE org_memberships 
                SET org_role = %s 
                WHERE user_id = %s AND org_id = %s;
            """, (role, str(user_id), int(org_id)))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-ORG-ERROR] Role promotion failed: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

# --- DYNAMIC GEOGRAPHIC LEAGUE ANALYTICS ---

def db_get_city_leaderboard():
    """Retrieves top performing cities based on collective student scores."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(o.city, u.personal_city) AS city, SUM(u.total_marks) as total_score
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id
                LEFT JOIN organizations o ON m.org_id = o.org_id
                GROUP BY city
                ORDER BY total_score DESC
                LIMIT 5;
            """)
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch city leaderboard: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_country_leaderboard():
    """Retrieves top performing countries based on collective student scores."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(o.country, u.personal_country) AS country, SUM(u.total_marks) as total_score
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id
                LEFT JOIN organizations o ON m.org_id = o.org_id
                GROUP BY country
                ORDER BY total_score DESC
                LIMIT 5;
            """)
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch country leaderboard: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

# --- HIERARCHICAL DRILL-DOWN: World -> Country -> City -> School -> Grade ---

def db_get_countries_ranked(limit: int = 15, offset: int = 0):
    """Ranked by the ledger (user_geo_contributions), not live personal_country — a student
    who's since moved still leaves their historical marks counted here for the OLD country."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT geo_value AS country, SUM(marks)::int AS total_score,
                       COUNT(DISTINCT user_id) AS student_count
                FROM user_geo_contributions
                WHERE geo_type = 'country'
                GROUP BY geo_value
                ORDER BY total_score DESC
                LIMIT %s OFFSET %s;
            """, (limit, offset))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch countries ranked: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_world_summary_counts(grade: int = None):
    """Tier-1 header block: total students/teams/schools/cities/countries + total/avg marks,
    optionally scoped to one grade."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(DISTINCT u.user_id) AS student_count,
                    COALESCE(SUM(u.total_marks), 0) AS total_marks,
                    COALESCE(AVG(u.total_marks), 0) AS avg_marks,
                    COUNT(DISTINCT COALESCE(o.country, u.personal_country)) AS country_count,
                    COUNT(DISTINCT COALESCE(o.city, u.personal_city)) AS city_count
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE (%s::int IS NULL OR u.grade = %s);
            """, (grade, grade))
            summary = dict(cur.fetchone())

            cur.execute("""
                SELECT COUNT(DISTINCT o.org_id) AS school_count, COUNT(DISTINCT b.branch_id) AS team_count
                FROM organizations o
                LEFT JOIN org_memberships m ON o.org_id = m.org_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN user_stats u ON m.user_id = u.user_id AND (%s::int IS NULL OR u.grade = %s)
                LEFT JOIN school_branches b ON b.org_id = o.org_id AND b.deleted_at IS NULL
                WHERE o.deleted_at IS NULL AND (%s::int IS NULL OR u.grade IS NOT NULL);
            """, (grade, grade, grade))
            counts = cur.fetchone()
            summary["school_count"] = counts["school_count"] if counts else 0
            summary["team_count"] = counts["team_count"] if counts else 0
            return summary
    except Exception as e:
        print(f"[DB ERROR] db_get_world_summary_counts: {e}", flush=True)
        return {"student_count": 0, "total_marks": 0, "avg_marks": 0, "country_count": 0, "city_count": 0, "school_count": 0, "team_count": 0}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_world_rank_matrix(grade: int = None, mode: str = "total", limit: int = 10):
    """
    Tier-1 multi-column table: top N students, teams (branches), schools, cities, countries.
    mode: 'total' sorts/scores by SUM(total_marks); 'average' sorts/scores by AVG(total_marks).
    Returns dict with keys: students, teams, schools, cities, countries — each a list of
    {'name': str, 'score': int}.
    """
    agg = "AVG" if mode == "average" else "SUM"
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            # Students
            cur.execute(f"""
                SELECT u.user_id, u.nickname, u.username, u.first_name, u.public_consent_granted,
                       u.total_marks AS score
                FROM user_stats u
                WHERE (%s::int IS NULL OR u.grade = %s)
                ORDER BY u.total_marks DESC
                LIMIT %s;
            """, (grade, grade, limit))
            students = [{"name": format_public_name(dict(r)), "score": r["score"]} for r in cur.fetchall()]

            # Teams (school_branches)
            cur.execute(f"""
                SELECT b.branch_name AS name, {agg}(u.total_marks)::int AS score
                FROM school_branches b
                JOIN org_memberships m ON m.branch_id = b.branch_id AND m.org_role NOT IN ('pending','rejected','left')
                JOIN user_stats u ON u.user_id = m.user_id
                WHERE b.deleted_at IS NULL AND (%s::int IS NULL OR u.grade = %s)
                GROUP BY b.branch_id, b.branch_name
                ORDER BY score DESC
                LIMIT %s;
            """, (grade, grade, limit))
            teams = [dict(r) for r in cur.fetchall()]

            # Schools
            cur.execute(f"""
                SELECT o.org_name AS name, {agg}(u.total_marks)::int AS score
                FROM organizations o
                JOIN org_memberships m ON m.org_id = o.org_id AND m.org_role NOT IN ('pending','rejected','left')
                JOIN user_stats u ON u.user_id = m.user_id
                WHERE o.deleted_at IS NULL AND (%s::int IS NULL OR u.grade = %s)
                GROUP BY o.org_id, o.org_name
                ORDER BY score DESC
                LIMIT %s;
            """, (grade, grade, limit))
            schools = [dict(r) for r in cur.fetchall()]

            # Cities
            cur.execute(f"""
                SELECT COALESCE(o.city, u.personal_city) AS name, {agg}(u.total_marks)::int AS score
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE COALESCE(o.city, u.personal_city) IS NOT NULL AND (%s::int IS NULL OR u.grade = %s)
                GROUP BY name
                ORDER BY score DESC
                LIMIT %s;
            """, (grade, grade, limit))
            cities = [dict(r) for r in cur.fetchall()]

            # Countries
            cur.execute(f"""
                SELECT COALESCE(o.country, u.personal_country) AS name, {agg}(u.total_marks)::int AS score
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE COALESCE(o.country, u.personal_country) IS NOT NULL AND (%s::int IS NULL OR u.grade = %s)
                GROUP BY name
                ORDER BY score DESC
                LIMIT %s;
            """, (grade, grade, limit))
            countries = [dict(r) for r in cur.fetchall()]

            return {"students": students, "teams": teams, "schools": schools, "cities": cities, "countries": countries}
    except Exception as e:
        print(f"[DB ERROR] db_get_world_rank_matrix: {e}", flush=True)
        return {"students": [], "teams": [], "schools": [], "cities": [], "countries": []}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_count_countries_ranked() -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(DISTINCT COALESCE(o.country, u.personal_country)) AS cnt
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE COALESCE(o.country, u.personal_country) IS NOT NULL;
            """)
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count countries: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_country_detail(country: str):
    """Returns this country's world rank/score plus every city within it, ranked — all from
    the frozen ledger, not live personal_country/personal_city."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT geo_value AS country, SUM(marks)::int AS total_score,
                           RANK() OVER (ORDER BY SUM(marks) DESC) AS world_rank
                    FROM user_geo_contributions WHERE geo_type = 'country'
                    GROUP BY geo_value
                )
                SELECT * FROM ranked WHERE country = %s;
            """, (country,))
            summary = cur.fetchone()

            # Cities within this country: join each student's city-ledger row to whichever
            # country-ledger row they also have, scoped to this country. Two students who
            # both contributed to "Addis Ababa" while in different countries (moved) stay
            # correctly separated because the join is per-user, not just by name.
            cur.execute("""
                SELECT gc.geo_value AS city, SUM(gc.marks)::int AS total_score,
                       COUNT(DISTINCT gc.user_id) AS student_count
                FROM user_geo_contributions gc
                JOIN user_geo_contributions gco
                  ON gco.user_id = gc.user_id AND gco.geo_type = 'country' AND gco.geo_value = %s
                WHERE gc.geo_type = 'city'
                GROUP BY gc.geo_value
                ORDER BY total_score DESC
                LIMIT 15;
            """, (country,))
            cities = cur.fetchall()
            return {"summary": dict(summary) if summary else {"total_score": 0, "world_rank": None}, "cities": cities}
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch country detail: {e}", flush=True)
        return {"summary": {"total_score": 0, "world_rank": None}, "cities": []}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_city_detail(city: str, country: str = None):
    """Returns this city's world + country rank/score plus every school within it, ranked —
    city/country totals from the geo ledger, school totals from the org ledger."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT geo_value AS city, SUM(marks)::int AS total_score,
                           RANK() OVER (ORDER BY SUM(marks) DESC) AS world_rank
                    FROM user_geo_contributions WHERE geo_type = 'city'
                    GROUP BY geo_value
                )
                SELECT * FROM ranked WHERE city = %s;
            """, (city,))
            summary = cur.fetchone()

            country_rank = None
            if country:
                cur.execute("""
                    WITH ranked AS (
                        SELECT gc.geo_value AS city, SUM(gc.marks)::int AS total_score,
                               RANK() OVER (ORDER BY SUM(gc.marks) DESC) AS country_rank
                        FROM user_geo_contributions gc
                        JOIN user_geo_contributions gco
                          ON gco.user_id = gc.user_id AND gco.geo_type = 'country' AND gco.geo_value = %s
                        WHERE gc.geo_type = 'city'
                        GROUP BY gc.geo_value
                    )
                    SELECT country_rank FROM ranked WHERE city = %s;
                """, (country, city))
                r = cur.fetchone()
                country_rank = r['country_rank'] if r else None

            cur.execute("""
                SELECT o.org_id, o.org_name, o.org_tag, SUM(c.marks)::int AS total_score,
                       COUNT(DISTINCT c.user_id) AS student_count
                FROM organizations o
                JOIN user_org_contributions c ON c.org_id = o.org_id
                WHERE o.city = %s AND o.deleted_at IS NULL
                GROUP BY o.org_id, o.org_name, o.org_tag
                ORDER BY total_score DESC
                LIMIT 15;
            """, (city,))
            schools = cur.fetchall()
            summary_dict = dict(summary) if summary else {"total_score": 0, "world_rank": None}
            summary_dict["country_rank"] = country_rank
            return {"summary": summary_dict, "schools": schools}
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch city detail: {e}", flush=True)
        return {"summary": {"total_score": 0, "world_rank": None, "country_rank": None}, "schools": []}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_schools_ranked(city: str = None, country: str = None, limit: int = 15, offset: int = 0):
    """Alphabetical school listing, optionally scoped to a city or country. Score is the
    frozen org-contribution ledger total, not live member sums."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT o.org_id, o.org_name, o.org_tag, o.city, o.country,
                       COALESCE(SUM(c.marks), 0)::int AS total_score
                FROM organizations o
                LEFT JOIN user_org_contributions c ON c.org_id = o.org_id
                WHERE o.deleted_at IS NULL
                  AND (%s::text IS NULL OR o.city = %s)
                  AND (%s::text IS NULL OR o.country = %s)
                GROUP BY o.org_id, o.org_name, o.org_tag, o.city, o.country
                ORDER BY o.org_name ASC
                LIMIT %s OFFSET %s;
            """, (city, city, country, country, limit, offset))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch schools ranked: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_count_schools_ranked(city: str = None, country: str = None) -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) AS cnt FROM organizations o
                WHERE o.deleted_at IS NULL
                  AND (%s::text IS NULL OR o.city = %s)
                  AND (%s::text IS NULL OR o.country = %s);
            """, (city, city, country, country))
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count schools ranked: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_org_rank_summary(org_id: int):
    """Returns this school's rank within its own city, its own country, and the whole world."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT city, country FROM organizations WHERE org_id = %s;", (int(org_id),))
            org = cur.fetchone()
            if not org:
                return {"city_rank": None, "country_rank": None, "world_rank": None}

            def _rank(scope_clause, scope_params):
                # Ranked by the ledger (user_org_contributions), which is frozen per-team —
                # never by live u.total_marks, which would double-count a student on 2 teams.
                cur.execute(f"""
                    WITH ranked AS (
                        SELECT o.org_id, SUM(c.marks) AS total_score,
                               RANK() OVER (ORDER BY SUM(c.marks) DESC) AS rnk
                        FROM organizations o
                        JOIN user_org_contributions c ON c.org_id = o.org_id
                        WHERE o.deleted_at IS NULL {scope_clause}
                        GROUP BY o.org_id
                    )
                    SELECT rnk FROM ranked WHERE org_id = %s;
                """, (*scope_params, int(org_id)))
                r = cur.fetchone()
                return r['rnk'] if r else None

            world_rank = _rank("", ())
            city_rank = _rank("AND o.city = %s", (org['city'],)) if org.get('city') else None
            country_rank = _rank("AND o.country = %s", (org['country'],)) if org.get('country') else None
            return {"city_rank": city_rank, "country_rank": country_rank, "world_rank": world_rank}
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch org rank summary: {e}", flush=True)
        return {"city_rank": None, "country_rank": None, "world_rank": None}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_org_contribution_total(org_id: int) -> int:
    """The team's ledger total — frozen contributions from every member who ever earned marks
    while active on this team, including ones who've since left (their marks stay counted here,
    they just stop growing)."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COALESCE(SUM(marks), 0)::int AS total FROM user_org_contributions WHERE org_id = %s;", (int(org_id),))
            row = cur.fetchone()
            return int(row['total']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] db_get_org_contribution_total: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_grade_world_ranked(limit: int = 10, offset: int = 0):
    """All registered grades, ranked by combined student marks — the entry list for the
    grade drill-down, mirroring db_get_countries_ranked."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT grade, SUM(total_marks) AS total_score, COUNT(*) AS student_count
                FROM user_stats
                WHERE grade IS NOT NULL
                GROUP BY grade
                ORDER BY total_score DESC
                LIMIT %s OFFSET %s;
            """, (limit, offset))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch grade world ranking: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_grade_detail(grade: int):
    """This grade's world rank/score, plus which countries and cities are strongest
    specifically AT this grade — the 'compare with city/country' requirement. Every
    total here is scoped to this one grade, not the country/city's overall score."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                WITH ranked AS (
                    SELECT grade, SUM(total_marks) AS total_score,
                           RANK() OVER (ORDER BY SUM(total_marks) DESC) AS world_rank
                    FROM user_stats WHERE grade IS NOT NULL GROUP BY grade
                )
                SELECT * FROM ranked WHERE grade = %s;
            """, (int(grade),))
            summary = cur.fetchone()

            cur.execute("""
                SELECT COALESCE(o.country, u.personal_country) AS country,
                       SUM(u.total_marks) AS total_score, COUNT(*) AS student_count
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE u.grade = %s AND COALESCE(o.country, u.personal_country) IS NOT NULL
                GROUP BY country
                ORDER BY total_score DESC
                LIMIT 5;
            """, (int(grade),))
            top_countries = cur.fetchall()

            cur.execute("""
                SELECT COALESCE(o.city, u.personal_city) AS city,
                       SUM(u.total_marks) AS total_score, COUNT(*) AS student_count
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE u.grade = %s AND COALESCE(o.city, u.personal_city) IS NOT NULL
                GROUP BY city
                ORDER BY total_score DESC
                LIMIT 5;
            """, (int(grade),))
            top_cities = cur.fetchall()

            return {
                "summary": dict(summary) if summary else {"total_score": 0, "world_rank": None},
                "top_countries": top_countries,
                "top_cities": top_cities,
            }
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch grade detail: {e}", flush=True)
        return {"summary": {"total_score": 0, "world_rank": None}, "top_countries": [], "top_cities": []}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

            
def db_update_user_location(user_id, city: str, country: str):
    """Sets the student's personal city/country AND auto-derives their timezone from the
    country (used for city/country leaderboards while unlinked from a team, and to render
    every DM-facing timestamp in their own local time)."""
    from src.geo import get_timezone_for_country
    tz_name = get_timezone_for_country(country)
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, personal_city, personal_country, timezone, total, correct, total_marks)
                VALUES (%s, %s, %s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET
                    personal_city = EXCLUDED.personal_city,
                    personal_country = EXCLUDED.personal_country,
                    timezone = EXCLUDED.timezone;
            """, (str(user_id), city, country, tz_name))
            user_profile_cache.invalidate(f"profile:{user_id}")
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update user location: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_cities_for_country(country: str):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT city FROM (
                    SELECT personal_city AS city FROM user_stats WHERE personal_country = %s AND personal_city IS NOT NULL
                    UNION
                    SELECT city FROM organizations WHERE country = %s AND city IS NOT NULL
                ) t
                WHERE city != ''
                ORDER BY city ASC
                LIMIT 60;
            """, (country, country))
            return [r['city'] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch cities for country: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_search_schools(query: str = None, city: str = None, country: str = None, limit: int = 8):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            clauses, params = ["deleted_at IS NULL", "status = 'approved'"], []
            if query:
                clauses.append("org_name ILIKE %s")
                params.append(f"%{query.strip()}%")
            if city:
                clauses.append("city = %s")
                params.append(city)
            if country:
                clauses.append("country = %s")
                params.append(country)
            params.append(limit)
            cur.execute(f"""
                SELECT org_id, org_name, org_tag, city, country
                FROM organizations
                WHERE {' AND '.join(clauses)}
                ORDER BY org_name ASC
                LIMIT %s;
            """, tuple(params))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to search schools: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_timezone(user_id) -> str:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT timezone FROM user_stats WHERE user_id = %s;", (str(user_id),))
            row = cur.fetchone()
            return row['timezone'] if row and row.get('timezone') else "UTC"
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user timezone: {e}", flush=True)
        return "UTC"
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_set_user_referrer(user_id, referrer_id):
    """
    Links a new user to whoever referred them — one-time only, no self-referral,
    referrer must already be a registered user. This only sets the link; the
    actual points come from fn_process_user_score (+1 mark to referrer per
    correct answer the referred user submits, capped at 2 tiers) — not from
    the act of recruiting itself, so it can't be gamed like a pyramid scheme.
    """
    conn = None
    try:
        if str(user_id) == str(referrer_id):
            return False
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM user_stats WHERE user_id = %s;", (str(referrer_id),))
            if not cur.fetchone():
                return False

            cur.execute("""
                INSERT INTO user_stats (user_id, referred_by, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET
                    referred_by = COALESCE(user_stats.referred_by, EXCLUDED.referred_by)
                WHERE user_stats.referred_by IS NULL;
            """, (str(user_id), str(referrer_id)))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set user referrer: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_submit_feedback(user_id, category: str, message: str) -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feedback (user_id, category, message)
                VALUES (%s, %s, %s) RETURNING id;
            """, (str(user_id), category, message))
            fid = cur.fetchone()['id']
            conn.commit()
            return fid
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to submit feedback: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_feedback_list(status: str = None, category: str = None, limit: int = 8, offset: int = 0):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            clauses, params = [], []
            if status:
                clauses.append("f.status = %s")
                params.append(status)
            if category:
                clauses.append("f.category = %s")
                params.append(category)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.extend([limit, offset])
            cur.execute(f"""
                SELECT f.*, us.nickname, us.username, us.first_name
                FROM feedback f
                LEFT JOIN user_stats us ON f.user_id = us.user_id
                {where}
                ORDER BY f.created_at DESC
                LIMIT %s OFFSET %s;
            """, tuple(params))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to list feedback: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_feedback_by_id(feedback_id: int):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT f.*, us.nickname, us.username, us.first_name
                FROM feedback f
                LEFT JOIN user_stats us ON f.user_id = us.user_id
                WHERE f.id = %s;
            """, (int(feedback_id),))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch feedback {feedback_id}: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)



def db_update_feedback_status(feedback_id: int, status: str) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE feedback SET status = %s, updated_at = NOW() WHERE id = %s;
            """, (status, int(feedback_id)))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update feedback status: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_save_feedback_reply(feedback_id: int, reply_text: str) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE feedback SET admin_reply = %s, status = 'in_progress', updated_at = NOW() WHERE id = %s;
            """, (reply_text, int(feedback_id)))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to save feedback reply: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_add_feedback_message(feedback_id: int, sender_role: str, sender_user_id, message: str) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO feedback_messages (feedback_id, sender_role, sender_user_id, message)
                VALUES (%s, %s, %s, %s);
            """, (int(feedback_id), sender_role, str(sender_user_id) if sender_user_id else None, message))

            if sender_role == "admin":
                cur.execute("UPDATE feedback SET admin_reply = %s, status = 'in_progress', updated_at = NOW() WHERE id = %s;", (message, int(feedback_id)))
            else:
                cur.execute("UPDATE feedback SET status = 'open', updated_at = NOW() WHERE id = %s;", (int(feedback_id),))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to add feedback message: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_feedback_thread(feedback_id: int):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM feedback_messages WHERE feedback_id = %s ORDER BY created_at ASC;", (int(feedback_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch feedback thread: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_feedback_stats():
    """Returns counts grouped by category and by status — powers the admin progress dashboard."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT category, COUNT(*) AS cnt FROM feedback GROUP BY category;")
            by_cat = {r['category']: r['cnt'] for r in cur.fetchall()}
            cur.execute("SELECT status, COUNT(*) AS cnt FROM feedback GROUP BY status;")
            by_status = {r['status']: r['cnt'] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS cnt FROM feedback;")
            total = cur.fetchone()['cnt']
            return {"by_category": by_cat, "by_status": by_status, "total": total}
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch feedback stats: {e}", flush=True)
        return {"by_category": {}, "by_status": {}, "total": 0}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_feedback_recent_by_status(limit_per_status: int = 3):
    """Returns up to `limit_per_status` most-recently-updated items per status column —
    powers the admin Kanban board in a single query instead of one per column."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, category, message, status, updated_at, user_id
                FROM (
                    SELECT f.*, ROW_NUMBER() OVER (PARTITION BY f.status ORDER BY f.updated_at DESC) AS rn
                    FROM feedback f
                ) ranked
                WHERE rn <= %s
                ORDER BY status, updated_at DESC;
            """, (limit_per_status,))
            rows = cur.fetchall()
            by_status = {}
            for r in rows:
                by_status.setdefault(r['status'], []).append(r)
            return by_status
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch feedback kanban rows: {e}", flush=True)
        return {}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_feedback_list(user_id, limit: int = 5, offset: int = 0):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM feedback
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s;
            """, (str(user_id), limit, offset))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user feedback list: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_count_user_feedback(user_id) -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS cnt FROM feedback WHERE user_id = %s;", (str(user_id),))
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count user feedback: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_admin_dashboard_stats():
    """Aggregates admin-facing platform stats. Each section is isolated so a single
    failing query (e.g. a missing column after a partial migration) doesn't blank
    the whole dashboard — it just omits that section and logs the specific cause."""
    conn = None
    result = {
        "total_users": 0,
        "by_country": [],
        "total_questions": 0,
        "by_subject": [],
        "total_orgs": 0,
        "total_responses": 0,
    }
    try:
        conn = GLOBAL_ENGINE.get_db_connection()

        with conn.cursor() as cur:
            try:
                cur.execute("SELECT COUNT(*) AS cnt FROM user_stats;")
                result["total_users"] = cur.fetchone()["cnt"]
            except Exception as e:
                print(f"[DB ERROR] admin_dashboard total_users: {e}", flush=True)

            try:
                cur.execute("""
                    SELECT COALESCE(o.country, u.personal_country, 'Unknown') AS country, COUNT(DISTINCT u.user_id) AS cnt
                    FROM user_stats u
                    LEFT JOIN org_memberships m ON u.user_id = m.user_id
                    LEFT JOIN organizations o ON m.org_id = o.org_id
                    GROUP BY country
                    ORDER BY cnt DESC
                    LIMIT 10;
                """)
                result["by_country"] = cur.fetchall()
            except Exception as e:
                print(f"[DB ERROR] admin_dashboard by_country: {e}", flush=True)

            try:
                cur.execute("SELECT COUNT(*) AS cnt FROM questions;")
                result["total_questions"] = cur.fetchone()["cnt"]
            except Exception as e:
                print(f"[DB ERROR] admin_dashboard total_questions: {e}", flush=True)

            try:
                cur.execute("SELECT subject, COUNT(*) AS cnt FROM questions GROUP BY subject ORDER BY cnt DESC;")
                result["by_subject"] = cur.fetchall()
            except Exception as e:
                print(f"[DB ERROR] admin_dashboard by_subject: {e}", flush=True)

            try:
                cur.execute("SELECT COUNT(*) AS cnt FROM organizations;")
                result["total_orgs"] = cur.fetchone()["cnt"]
            except Exception as e:
                print(f"[DB ERROR] admin_dashboard total_orgs: {e}", flush=True)

            try:
                cur.execute("SELECT COUNT(*) AS cnt FROM user_responses;")
                result["total_responses"] = cur.fetchone()["cnt"]
            except Exception as e:
                print(f"[DB ERROR] admin_dashboard total_responses: {e}", flush=True)

        return result
    except Exception as e:
        print(f"[DB ERROR] admin_dashboard connection failure: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_recent_users(limit: int = 15, offset: int = 0):
    """Retrieves the most recently active users with key profile details for the admin dashboard."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.user_id, u.username, u.first_name, u.nickname, u.grade, u.total_marks,
                       u.public_consent_granted,
                       COALESCE(o.country, u.personal_country, 'Unknown') AS country,
                       COALESCE(o.city, u.personal_city, 'Unknown') AS city,
                       u.last_active_at
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id
                LEFT JOIN organizations o ON m.org_id = o.org_id
                ORDER BY u.last_active_at DESC NULLS LAST
                LIMIT %s OFFSET %s;
            """, (limit, offset))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch recent users: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_or_create_referral_token(user_id) -> str:
    """Returns the user's opaque referral token, generating one if missing."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT referral_token FROM user_stats WHERE user_id = %s;", (str(user_id),))
            row = cur.fetchone()
            if row and row.get("referral_token"):
                return row["referral_token"]

            token = secrets.token_hex(16)
            cur.execute("""
                INSERT INTO user_stats (user_id, referral_token, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET referral_token = EXCLUDED.referral_token
                WHERE user_stats.referral_token IS NULL;
            """, (str(user_id), token))
            conn.commit()
            return token
    except Exception as e:
        print(f"[DB ERROR] Failed to get/create referral token: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_user_id_by_referral_token(token: str):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM user_stats WHERE referral_token = %s;", (token,))
            row = cur.fetchone()
            return row["user_id"] if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to resolve referral token: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)



def db_claim_admin(user_id) -> bool:
    """Grants admin status. Only ever called after the secret has already been verified by the caller."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, is_admin, total, correct, total_marks)
                VALUES (%s, TRUE, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET is_admin = TRUE;
            """, (str(user_id),))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to claim admin: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_is_admin(user_id) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT is_admin FROM user_stats WHERE user_id = %s;", (str(user_id),))
            row = cur.fetchone()
            return bool(row and row.get("is_admin"))
    except Exception as e:
        print(f"[DB ERROR] Failed to check admin status: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_cooldown_stats(subject: str = None):
    """
    Explains *why* the scheduling pool came back empty instead of leaving the
    admin guessing: total question count in scope, how many have never been
    shown at all, and how many days ago the least-recently-shown question was
    last sent (i.e. the largest cooldown value that would currently return
    zero rows). Powers the Smart Scheduler's fallback diagnostic.
    """
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            params = []
            clause = ""
            if subject:
                clause = "WHERE lower(subject) = lower(%s)"
                params.append(subject)

            cur.execute(f"SELECT COUNT(*) AS total FROM questions {clause};", tuple(params))
            total = cur.fetchone()["total"]

            cur.execute(f"""
                SELECT MIN(EXTRACT(EPOCH FROM (NOW() - last_shown_at)) / 86400.0) AS min_days_since_shown,
                       COUNT(*) FILTER (WHERE last_shown_at IS NULL) AS never_shown
                FROM questions {clause};
            """, tuple(params))
            row = cur.fetchone()

            return {
                "total": total,
                "never_shown": row["never_shown"] or 0,
                "min_days_since_shown": float(row["min_days_since_shown"]) if row["min_days_since_shown"] is not None else None,
            }
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch cooldown stats: {e}", flush=True)
        return {"total": 0, "never_shown": 0, "min_days_since_shown": None}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

async def db_call_guarded(fn, *args, **kwargs):
    """Runs a sync DB function in a thread, but bounded by DB_SEMAPHORE so a traffic
    spike queues politely instead of exhausting the connection pool and hanging every
    request indefinitely. Raises TimeoutError if the queue itself is too backed up."""
    try:
        async with _asyncio.timeout(8.0):
            async with DB_SEMAPHORE:
                return await _asyncio.to_thread(fn, *args, **kwargs)
    except TimeoutError:
        print(f"[DB-OVERLOAD] Call to {fn.__name__} timed out waiting for DB capacity.", flush=True)
        raise

def db_get_all_admin_ids():
    """Returns a list of user_ids flagged as admin. Used to notify admins of new feedback."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM user_stats WHERE is_admin = TRUE;")
            return [r["user_id"] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch admin ids: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_tournament_leaderboard(run_id: str, limit: int = 10):
    """Ranking scoped ONLY to this tournament run — sums marks across every round
    tagged with the same tournament_run_id, not the user's all-time/weekly total."""
    if not run_id:
        return []
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ur.user_id, SUM(ur.marks_awarded) AS tournament_score,
                       COUNT(*) FILTER (WHERE ur.is_correct) AS tournament_correct,
                       us.nickname, us.username, us.first_name, us.public_consent_granted,
                       (SELECT o.org_tag FROM organizations o JOIN org_memberships m ON o.org_id = m.org_id WHERE m.user_id = ur.user_id LIMIT 1) AS alliance_tag
                FROM user_responses ur
                JOIN sent_tracks st ON ur.message_id = st.message_id
                JOIN user_stats us ON ur.user_id = us.user_id
                WHERE st.tournament_run_id = %s
                GROUP BY ur.user_id, us.nickname, us.username, us.first_name, us.public_consent_granted
                ORDER BY tournament_score DESC
                LIMIT %s;
            """, (run_id, limit))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch tournament leaderboard: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_tournament_geo_leaderboard(run_id: str, group_by: str, limit: int = 5):
    """Tournament-scoped city/country/school standings — sums marks_awarded across every
    round tagged with this tournament_run_id ONLY, never the lifetime/all-time totals.
    group_by: 'city' | 'country' | 'school'."""
    if not run_id:
        return []
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            if group_by == "school":
                cur.execute("""
                    SELECT o.org_name AS label, SUM(ur.marks_awarded) AS total_score
                    FROM user_responses ur
                    JOIN sent_tracks st ON ur.message_id = st.message_id
                    JOIN org_memberships m ON m.user_id = ur.user_id
                    JOIN organizations o ON o.org_id = m.org_id
                    WHERE st.tournament_run_id = %s AND ur.is_correct = TRUE
                    GROUP BY o.org_name
                    ORDER BY total_score DESC
                    LIMIT %s;
                """, (run_id, limit))
            else:
                geo_col = "o.city" if group_by == "city" else "o.country"
                personal_col = "us.personal_city" if group_by == "city" else "us.personal_country"
                cur.execute(f"""
                    SELECT COALESCE({geo_col}, {personal_col}) AS label, SUM(ur.marks_awarded) AS total_score
                    FROM user_responses ur
                    JOIN sent_tracks st ON ur.message_id = st.message_id
                    JOIN user_stats us ON us.user_id = ur.user_id
                    LEFT JOIN org_memberships m ON m.user_id = ur.user_id
                    LEFT JOIN organizations o ON o.org_id = m.org_id
                    WHERE st.tournament_run_id = %s AND ur.is_correct = TRUE
                    GROUP BY label
                    ORDER BY total_score DESC
                    LIMIT %s;
                """, (run_id, limit))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch tournament geo leaderboard ({group_by}): {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_create_location_suggestion(kind: str, name: str, country: str, submitted_by, org_id: int = None) -> int:
    from src.geo import normalize_location_name
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            norm = normalize_location_name(name)
            cur.execute("""
                SELECT id FROM location_suggestions
                WHERE kind = %s AND normalized_name = %s AND submitted_by = %s AND status = 'pending';
            """, (kind, norm, str(submitted_by)))
            existing = cur.fetchone()
            if existing:
                cur.execute("""
                    UPDATE location_suggestions
                    SET request_count = COALESCE(request_count, 1) + 1, last_requested_at = NOW()
                    WHERE id = %s;
                """, (existing['id'],))
                conn.commit()
                return existing['id']

            cur.execute("""
                INSERT INTO location_suggestions (kind, name, normalized_name, country, submitted_by, org_id, request_count, last_requested_at)
                VALUES (%s, %s, %s, %s, %s, %s, 1, NOW()) RETURNING id;
            """, (kind, name, norm, country, str(submitted_by), org_id))
            sid = cur.fetchone()['id']
            conn.commit()
            return sid
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to create location suggestion: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def _generate_unique_org_tag(org_name: str) -> str:
    """Auto-derives a short uppercase tag from a school name so users registering a school
    through the location wizard are never asked to type one. Falls back to a random suffix
    on collision instead of failing the unique constraint."""
    import re as _re
    import secrets as _secrets
    base = _re.sub(r'[^A-Za-z0-9]', '', org_name).upper()[:12] or "SCHOOL"
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            candidate = base
            cur.execute("SELECT 1 FROM organizations WHERE org_tag = %s;", (candidate,))
            attempt = 0
            while cur.fetchone():
                attempt += 1
                suffix = _secrets.token_hex(2).upper()
                candidate = f"{(base[:12-len(suffix)-1] or base[:1])}{suffix}"
                cur.execute("SELECT 1 FROM organizations WHERE org_tag = %s;", (candidate,))
                if attempt > 5:
                    candidate = _secrets.token_hex(6).upper()
                    break
            return candidate
    except Exception as e:
        print(f"[DB ERROR] Failed to generate unique org tag: {e}", flush=True)
        return f"{base[:8]}{_secrets.token_hex(2).upper()}"
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

            
def db_get_location_suggestions_list(status: str = None, kind: str = None, limit: int = 8, offset: int = 0):
    """Admin inbox listing, newest request first. status/kind = None means 'all'."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            clauses, params = [], []
            if status and status != "all":
                clauses.append("ls.status = %s")
                params.append(status)
            if kind and kind != "all":
                clauses.append("ls.kind = %s")
                params.append(kind)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            params.extend([limit, offset])
            cur.execute(f"""
                SELECT ls.*, us.nickname, us.username, us.first_name
                FROM location_suggestions ls
                LEFT JOIN user_stats us ON ls.submitted_by = us.user_id
                {where}
                ORDER BY COALESCE(ls.last_requested_at, ls.created_at) DESC
                LIMIT %s OFFSET %s;
            """, tuple(params))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to list location suggestions: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_count_location_suggestions(status: str = None, kind: str = None) -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            clauses, params = [], []
            if status and status != "all":
                clauses.append("status = %s")
                params.append(status)
            if kind and kind != "all":
                clauses.append("kind = %s")
                params.append(kind)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cur.execute(f"SELECT COUNT(*) AS cnt FROM location_suggestions {where};", tuple(params))
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count location suggestions: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_location_suggestion(suggestion_id: int):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM location_suggestions WHERE id = %s;", (int(suggestion_id),))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch location suggestion: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_pending_location_suggestions(kind: str = None, limit: int = 20):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            if kind:
                cur.execute("SELECT * FROM location_suggestions WHERE status = 'pending' AND kind = %s ORDER BY created_at ASC LIMIT %s;", (kind, limit))
            else:
                cur.execute("SELECT * FROM location_suggestions WHERE status = 'pending' ORDER BY created_at ASC LIMIT %s;", (limit,))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch pending location suggestions: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_resolve_location_suggestion(suggestion_id: int, admin_id, approve: bool) -> dict:
    """Approves or rejects a city/school suggestion.

    Reject (city): the city is REMOVED from the profile entirely (city/country/status/pending-id
    all cleared) so db_user_location_complete() re-triggers and blocks new answers until they
    register again — never left half-set with a rejected value silently lingering.

    Reject (school): the requesting student is removed from that org and the org itself is
    soft-deleted — it only ever existed for this one pending review and was never a real,
    approved team — so the student is back to "not a student" and free to try again.
    """
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM location_suggestions WHERE id = %s FOR UPDATE;", (int(suggestion_id),))
            sug = cur.fetchone()
            if not sug:
                return None

            new_status = "approved" if approve else "rejected"
            cur.execute("""
                UPDATE location_suggestions SET status = %s, admin_id = %s, resolved_at = NOW()
                WHERE id = %s;
            """, (new_status, str(admin_id), int(suggestion_id)))

            affected_users = []
            if sug['kind'] == 'city':
                if approve:
                    cur.execute("""
                        UPDATE user_stats SET personal_city_status = 'approved'
                        WHERE pending_city_suggestion_id = %s;
                    """, (int(suggestion_id),))
                else:
                    cur.execute("""
                        UPDATE user_stats
                        SET personal_city = NULL, personal_country = NULL,
                            personal_city_status = NULL, pending_city_suggestion_id = NULL
                        WHERE pending_city_suggestion_id = %s;
                    """, (int(suggestion_id),))
                affected_users = [sug['submitted_by']]
            elif sug['kind'] == 'school' and sug.get('org_id'):
                if approve:
                    cur.execute("UPDATE organizations SET status = 'approved' WHERE org_id = %s;", (sug['org_id'],))
                else:
                    cur.execute("UPDATE organizations SET status = 'rejected', deleted_at = NOW() WHERE org_id = %s;", (sug['org_id'],))
                    cur.execute("""
                        UPDATE org_memberships SET org_role = 'left', deleted_at = NOW()
                        WHERE user_id = %s AND org_id = %s;
                    """, (sug['submitted_by'], sug['org_id']))
                affected_users = [sug['submitted_by']]

            conn.commit()
            return {"suggestion": dict(sug), "affected_users": affected_users}
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to resolve location suggestion: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_user_location_complete(user_id) -> bool:
    """Gate check: a student must have BOTH a city and a country on file — approved OR still
    pending review, either counts — before submitting a new answer. A rejected city is cleared
    entirely by db_resolve_location_suggestion above, which is exactly what re-triggers this
    gate. Fails OPEN on a DB error — a hiccup here should never lock out every student at once."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT personal_city, personal_country FROM user_stats WHERE user_id = %s;", (str(user_id),))
            row = cur.fetchone()
            if not row:
                return False
            return bool(row.get("personal_city")) and bool(row.get("personal_country"))
    except Exception as e:
        print(f"[DB ERROR] db_user_location_complete: {e}", flush=True)
        return True
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_add_location_suggestion_message(suggestion_id: int, sender_role: str, sender_user_id, message: str) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO location_suggestion_messages (suggestion_id, sender_role, sender_user_id, message)
                VALUES (%s, %s, %s, %s);
            """, (int(suggestion_id), sender_role, str(sender_user_id) if sender_user_id else None, message))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to add location suggestion message: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_location_suggestion_thread(suggestion_id: int):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM location_suggestion_messages WHERE suggestion_id = %s ORDER BY created_at ASC;", (int(suggestion_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch suggestion thread: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_set_user_pending_city(user_id, city: str, country: str, suggestion_id: int) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, personal_city, personal_country, personal_city_status, pending_city_suggestion_id, total, correct, total_marks)
                VALUES (%s, %s, %s, 'pending', %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET
                    personal_city = EXCLUDED.personal_city,
                    personal_country = EXCLUDED.personal_country,
                    personal_city_status = 'pending',
                    pending_city_suggestion_id = EXCLUDED.pending_city_suggestion_id;
            """, (str(user_id), city, country, suggestion_id))
            user_profile_cache.invalidate(f"profile:{user_id}")
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set pending city: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_all_admin_ids_cached():
    return db_get_all_admin_ids()

def db_count_feedback(status: str = None, category: str = None) -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            clauses, params = [], []
            if status:
                clauses.append("status = %s")
                params.append(status)
            if category:
                clauses.append("category = %s")
                params.append(category)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            cur.execute(f"SELECT COUNT(*) AS cnt FROM feedback {where};", tuple(params))
            row = cur.fetchone()
            return int(row['cnt']) if row else 0
    except Exception as e:
        print(f"[DB ERROR] Failed to count feedback: {e}", flush=True)
        return 0
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_bot_state(key: str, default=None):
    """Reads a small persisted admin setting (e.g. cleanup timers, the currently
    pinned champions-podium message id). Backed by a tiny key/value table so
    these survive restarts and tournament-queue clears."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM bot_state WHERE key = %s;", (key,))
            row = cur.fetchone()
            if not row:
                return default
            val = row["value"]
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except Exception:
                    pass
            return val
    except Exception as e:
        print(f"[DB ERROR] Failed to get bot_state[{key}]: {e}", flush=True)
        return default
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_last_utility_mid(user_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT last_utility_mid FROM user_stats WHERE user_id = %s;", (str(user_id),))
            row = cur.fetchone()
            return row["last_utility_mid"] if row else None
    except Exception as e:
        print(f"[DB ERROR] db_get_last_utility_mid: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_set_last_utility_mid(user_id, mid):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, last_utility_mid, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET last_utility_mid = EXCLUDED.last_utility_mid;
            """, (str(user_id), int(mid) if mid else None))
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] db_set_last_utility_mid: {e}", flush=True)
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_set_bot_state(key: str, value) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_state (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
            """, (key, Json(value)))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set bot_state[{key}]: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_create_campaign(name: str, html_content: str, pin_it: bool = True) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO channel_campaigns (name, html_content, pin_it)
                VALUES (%s, %s, %s);
            """, (name.strip(), html_content, pin_it))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to create campaign: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_all_campaigns(active_only: bool = False):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            clause = "WHERE is_active = TRUE AND deleted_at IS NULL" if active_only else "WHERE deleted_at IS NULL"
            cur.execute(f"SELECT * FROM channel_campaigns {clause} ORDER BY id ASC;")
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to list campaigns: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_campaign_by_name(name: str):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM channel_campaigns WHERE name = %s AND deleted_at IS NULL;", (name.strip(),))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch campaign: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_update_campaign_content(name: str, html_content: str) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE channel_campaigns SET html_content = %s, updated_at = NOW()
                WHERE name = %s;
            """, (html_content, name.strip()))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update campaign content: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_set_campaign_active(name: str, is_active: bool) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE channel_campaigns SET is_active = %s, updated_at = NOW()
                WHERE name = %s;
            """, (is_active, name.strip()))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to toggle campaign: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_set_campaign_schedule(name: str, schedule: dict) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE channel_campaigns SET schedule = %s, updated_at = NOW()
                WHERE name = %s;
            """, (Json(schedule), name.strip()))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set campaign schedule: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_set_campaign_posted_mid(name: str, mid) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE channel_campaigns SET posted_mid = %s WHERE name = %s;
            """, (int(mid) if mid else None, name.strip()))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set campaign posted_mid: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_delete_campaign(name: str) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("UPDATE channel_campaigns SET deleted_at = NOW() WHERE name = %s;", (name.strip(),))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to soft-delete campaign: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_org_admin_ids(org_id: int):
    """Creator + promoted admins — everyone who should be notified of join requests."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id FROM org_memberships
                WHERE org_id = %s AND org_role IN ('creator', 'admin');
            """, (int(org_id),))
            return [r['user_id'] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB-ORG-ERROR] Fetch admin ids failed: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_active_grades():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT grade FROM user_stats WHERE grade IS NOT NULL ORDER BY grade ASC;")
            return [r['grade'] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch active grades: {e}", flush=True)
        return [6, 8, 10, 12]
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_active_countries():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT COALESCE(o.country, u.personal_country) AS country
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE COALESCE(o.country, u.personal_country) IS NOT NULL
                ORDER BY country ASC
                LIMIT 40;
            """)
            return [r['country'] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch active countries: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_active_cities(country: str = None):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            if country:
                cur.execute("""
                    SELECT DISTINCT COALESCE(o.city, u.personal_city) AS city
                    FROM user_stats u
                    LEFT JOIN org_memberships m ON u.user_id = m.user_id
                    LEFT JOIN organizations o ON m.org_id = o.org_id
                    WHERE COALESCE(o.country, u.personal_country) = %s AND COALESCE(o.city, u.personal_city) IS NOT NULL
                    ORDER BY city ASC
                    LIMIT 40;
                """, (country,))
            else:
                cur.execute("""
                    SELECT DISTINCT COALESCE(o.city, u.personal_city) AS city
                    FROM user_stats u
                    LEFT JOIN org_memberships m ON u.user_id = m.user_id
                    LEFT JOIN organizations o ON m.org_id = o.org_id
                    WHERE COALESCE(o.city, u.personal_city) IS NOT NULL
                    ORDER BY city ASC
                    LIMIT 40;
                """)
            return [r['city'] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch active cities: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_top_users_by_city(city: str, limit: int = 10):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.user_id, u.nickname, u.username, u.first_name, u.public_consent_granted, u.total_marks
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE COALESCE(o.city, u.personal_city) = %s
                ORDER BY u.total_marks DESC
                LIMIT %s;
            """, (city, limit))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch top users by city: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_top_users_by_country(country: str, limit: int = 10):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.user_id, u.nickname, u.username, u.first_name, u.public_consent_granted, u.total_marks
                FROM user_stats u
                LEFT JOIN org_memberships m ON u.user_id = m.user_id
                LEFT JOIN organizations o ON m.org_id = o.org_id
                WHERE COALESCE(o.country, u.personal_country) = %s
                ORDER BY u.total_marks DESC
                LIMIT %s;
            """, (country, limit))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch top users by country: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_all_subjects():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT subject FROM questions WHERE subject IS NOT NULL ORDER BY subject ASC;")
            return [r['subject'] for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] db_get_all_subjects: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


# def db_get_rank_matrix(scope: str = "world", grade=None, limit: int = 10):
#     """Unified matrix for World/Country/City/School — always SUM(total_marks), grade optional."""
#     from src.rendering.html_views import format_public_name
#     conn = None
#     try:
#         conn = GLOBAL_ENGINE.get_db_connection()
#         with conn.cursor() as cur:
#             grade_val = None if grade in (None, "all") else int(grade)

#             cur.execute("""
#                 SELECT u.user_id, u.nickname, u.username, u.first_name, u.public_consent_granted, u.total_marks AS score
#                 FROM user_stats u
#                 WHERE (%s::int IS NULL OR u.grade = %s)
#                 ORDER BY u.total_marks DESC LIMIT %s;
#             """, (grade_val, grade_val, limit))
#             students = [{"name": format_public_name(dict(r)), "score": r["score"]} for r in cur.fetchall()]

#             def _group(label_col, join_clause, require_org=False):
#                 cur.execute(f"""
#                     SELECT {label_col} AS name, SUM(u.total_marks)::int AS score
#                     FROM user_stats u
#                     {join_clause}
#                     WHERE {label_col} IS NOT NULL AND (%s::int IS NULL OR u.grade = %s)
#                     GROUP BY name ORDER BY score DESC LIMIT %s;
#                 """, (grade_val, grade_val, limit))
#                 return [dict(r) for r in cur.fetchall()]

#             org_left = ("LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left') "
#                         "LEFT JOIN organizations o ON m.org_id = o.org_id")
#             org_req = ("JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left') "
#                        "JOIN organizations o ON m.org_id = o.org_id AND o.deleted_at IS NULL")

#             teams = _group("o.org_name", org_req)
#             schools = _group("o.org_name", org_req)
#             cities = _group("COALESCE(o.city, u.personal_city)", org_left)
#             countries = _group("COALESCE(o.country, u.personal_country)", org_left)

#             return {"students": students, "teams": teams, "schools": schools, "cities": cities, "countries": countries}
#     except Exception as e:
#         print(f"[DB ERROR] db_get_rank_matrix({scope}): {e}", flush=True)
#         return {"students": [], "teams": [], "schools": [], "cities": [], "countries": []}
#     finally:
#         if conn:
#             GLOBAL_ENGINE.release_connection(conn)
def db_get_rank_matrix(scope="world", entity=None, grade=None, subject=None, difficulty=None, mode="total", limit=10):
    """
    scope: world|country|city|school. entity: selected country/city name, or org_id (str) for school, or None.
    Combined filters (grade + subject + difficulty) apply together. mode: 'total' (SUM) or 'average' (AVG).
    """
    from src.rendering.html_views import format_public_name
    agg = "AVG" if mode == "average" else "SUM"
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            grade_val = None if grade in (None, "all") else int(grade)
            subject_val = None if subject in (None, "all") else subject.lower()
            difficulty_val = None if difficulty in (None, "all") else difficulty.lower()

            extra_join, score_col, extra_params = "", "u.total_marks", []
            if subject_val:
                extra_join = "JOIN user_subject_marks sm ON sm.user_id = u.user_id AND sm.subject = %s"
                extra_params = [subject_val]
                score_col = "sm.marks"
            elif difficulty_val:
                extra_join = "JOIN user_difficulty_marks dm ON dm.user_id = u.user_id AND dm.difficulty = %s"
                extra_params = [difficulty_val]
                score_col = "dm.marks"

            org_left = ("LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left') "
                        "LEFT JOIN organizations o ON m.org_id = o.org_id")
            org_req = ("JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left') "
                       "JOIN organizations o ON m.org_id = o.org_id AND o.deleted_at IS NULL")

            entity_clause, entity_params = "", []
            if scope == "country" and entity:
                entity_clause = "AND COALESCE(o.country, u.personal_country) = %s"
                entity_params = [entity]
            elif scope == "city" and entity:
                entity_clause = "AND COALESCE(o.city, u.personal_city) = %s"
                entity_params = [entity]
            elif scope == "school" and entity:
                entity_clause = "AND o.org_id = %s"
                entity_params = [int(entity)]

            join_for_scope = org_req if scope in ("city", "school") else org_left

            cur.execute(f"""
                SELECT u.user_id, u.nickname, u.username, u.first_name, u.public_consent_granted, {score_col} AS score
                FROM user_stats u
                {extra_join}
                {join_for_scope if entity_clause else ''}
                WHERE (%s::int IS NULL OR u.grade = %s) {entity_clause}
                ORDER BY score DESC LIMIT %s;
            """, tuple(extra_params) + (grade_val, grade_val) + tuple(entity_params) + (limit,))
            students = [{"name": format_public_name(dict(r)), "score": r["score"]} for r in cur.fetchall()]

            def _group(label_col, join_clause):
                cur.execute(f"""
                    SELECT {label_col} AS name, {agg}({score_col})::int AS score
                    FROM user_stats u
                    {extra_join}
                    {join_clause}
                    WHERE {label_col} IS NOT NULL AND (%s::int IS NULL OR u.grade = %s) {entity_clause}
                    GROUP BY name ORDER BY score DESC LIMIT %s;
                """, tuple(extra_params) + (grade_val, grade_val) + tuple(entity_params) + (limit,))
                return [dict(r) for r in cur.fetchall()]

            result = {"students": students}
            if scope in ("world", "country", "city"):
                result["schools"] = _group("o.org_name", org_req)
            if scope in ("world", "country", "city", "school"):
                result["teams"] = _group("o.org_name", org_req)
            if scope in ("world", "country"):
                result["cities"] = _group("COALESCE(o.city, u.personal_city)", org_left)
            if scope == "world":
                result["countries"] = _group("COALESCE(o.country, u.personal_country)", org_left)

            return result
    except Exception as e:
        print(f"[DB ERROR] db_get_rank_matrix({scope}/{entity}): {e}", flush=True)
        return {"students": []}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_scope_summary(scope="world", entity=None, grade=None):
    """Section-1 stats: population counts, total/avg score, and parent rank."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            grade_val = None if grade in (None, "all") else int(grade)
            org_left = ("LEFT JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left') "
                        "LEFT JOIN organizations o ON m.org_id = o.org_id")
            org_req = ("JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left') "
                       "JOIN organizations o ON m.org_id = o.org_id AND o.deleted_at IS NULL")

            entity_clause, entity_params, join_sql = "", [], org_left
            if scope == "country" and entity:
                entity_clause, entity_params = "AND COALESCE(o.country, u.personal_country) = %s", [entity]
            elif scope == "city" and entity:
                entity_clause, entity_params, join_sql = "AND COALESCE(o.city, u.personal_city) = %s", [entity], org_req
            elif scope == "school" and entity:
                entity_clause, entity_params, join_sql = "AND o.org_id = %s", [int(entity)], org_req

            cur.execute(f"""
                SELECT COUNT(DISTINCT u.user_id) AS student_count,
                       COALESCE(SUM(u.total_marks), 0) AS total_marks,
                       COALESCE(AVG(u.total_marks), 0) AS avg_marks,
                       COUNT(DISTINCT o.org_id) AS school_count,
                       COUNT(DISTINCT COALESCE(o.city, u.personal_city)) AS city_count,
                       COUNT(DISTINCT COALESCE(o.country, u.personal_country)) AS country_count
                FROM user_stats u
                {join_sql}
                WHERE (%s::int IS NULL OR u.grade = %s) {entity_clause};
            """, (grade_val, grade_val) + tuple(entity_params))
            summary = dict(cur.fetchone())
            summary["team_count"] = summary["school_count"]

            # City/country totals come from the frozen ledger, not the live student sum —
            # a student who's since moved elsewhere still counts here for what they earned
            # while they were part of this place, and doesn't count toward it going forward.
            if scope in ("country", "city") and entity:
                geo_type = "country" if scope == "country" else "city"
                cur.execute("""
                    SELECT COALESCE(SUM(marks), 0) AS total FROM user_geo_contributions
                    WHERE geo_type = %s AND geo_value = %s;
                """, (geo_type, entity))
                ledger_row = cur.fetchone()
                summary["total_marks"] = int(ledger_row["total"]) if ledger_row else summary["total_marks"]

            summary["parent_ranks"] = {}
            if scope == "country" and entity:
                cur.execute("""
                    WITH ranked AS (SELECT COALESCE(o.country, u.personal_country) AS c, SUM(u.total_marks) AS s,
                        RANK() OVER (ORDER BY SUM(u.total_marks) DESC) AS r
                        FROM user_stats u LEFT JOIN org_memberships m ON u.user_id=m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                        LEFT JOIN organizations o ON m.org_id=o.org_id WHERE COALESCE(o.country,u.personal_country) IS NOT NULL GROUP BY c)
                    SELECT r FROM ranked WHERE c = %s;
                """, (entity,))
                row = cur.fetchone()
                summary["parent_ranks"]["world"] = row["r"] if row else None
            elif scope == "city" and entity:
                cur.execute("SELECT country FROM organizations WHERE city = %s LIMIT 1;", (entity,))
                row = cur.fetchone()
                summary["parent_country"] = row["country"] if row else None
            elif scope == "school" and entity:
                cur.execute("SELECT city, country FROM organizations WHERE org_id = %s;", (int(entity),))
                row = cur.fetchone()
                summary["parent_city"] = row["city"] if row else None
                summary["parent_country"] = row["country"] if row else None

            return summary
    except Exception as e:
        print(f"[DB ERROR] db_get_scope_summary({scope}/{entity}): {e}", flush=True)
        return {"student_count": 0, "total_marks": 0, "avg_marks": 0, "school_count": 0, "team_count": 0, "city_count": 0, "country_count": 0}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_entity_list(scope, parent_entity=None, limit=40):
    """Entity picker source (📍 PICK COUNTRY/CITY/SCHOOL buttons)."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            if scope == "country":
                cur.execute("""
                    SELECT DISTINCT COALESCE(o.country, u.personal_country) AS name
                    FROM user_stats u LEFT JOIN org_memberships m ON u.user_id=m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                    LEFT JOIN organizations o ON m.org_id=o.org_id
                    WHERE COALESCE(o.country, u.personal_country) IS NOT NULL
                    ORDER BY name ASC LIMIT %s;
                """, (limit,))
            elif scope == "city":
                cur.execute("""
                    SELECT DISTINCT COALESCE(o.city, u.personal_city) AS name
                    FROM user_stats u LEFT JOIN org_memberships m ON u.user_id=m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                    LEFT JOIN organizations o ON m.org_id=o.org_id
                    WHERE COALESCE(o.country, u.personal_country) = %s AND COALESCE(o.city, u.personal_city) IS NOT NULL
                    ORDER BY name ASC LIMIT %s;
                """, (parent_entity, limit))
            elif scope == "school":
                cur.execute("""
                    SELECT org_id AS id, org_name AS name FROM organizations
                    WHERE city = %s AND deleted_at IS NULL ORDER BY org_name ASC LIMIT %s;
                """, (parent_entity, limit))
            else:
                return []
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        print(f"[DB ERROR] db_get_entity_list({scope}): {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


                      
def db_edit_tournament_answer(user_id, message_id, new_option: int, new_is_correct: bool):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM fn_edit_user_answer(%s::text, %s::text, %s::int, %s::boolean);",
                (str(user_id), str(message_id), int(new_option), bool(new_is_correct))
            )
            row = cur.fetchone()
            conn.commit()
            return dict(row) if row else None
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to edit tournament answer: {e}", flush=True)
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_get_user_edit_stats(user_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT answer_edits_total, answer_edits_helped, answer_edits_hurt FROM user_stats WHERE user_id = %s;",
                (str(user_id),)
            )
            row = cur.fetchone()
            return dict(row) if row else {"answer_edits_total": 0, "answer_edits_helped": 0, "answer_edits_hurt": 0}
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch edit stats: {e}", flush=True)
        return {"answer_edits_total": 0, "answer_edits_helped": 0, "answer_edits_hurt": 0}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_is_tournament_round_still_open(message_id) -> bool:
    """True only if the round for this message is still tournament_active and its deadline hasn't passed."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, EXTRACT(EPOCH FROM round_deadline) AS deadline_epoch, "
                "EXTRACT(EPOCH FROM NOW()) AS now_epoch FROM sent_tracks WHERE message_id = %s;",
                (str(message_id),)
            )
            row = cur.fetchone()
            if not row or row['status'] != 'tournament_active':
                return False
            if row['deadline_epoch'] is None:
                return False
            return float(row['deadline_epoch']) > float(row['now_epoch'])
    except Exception as e:
        print(f"[DB ERROR] Failed to check round open state: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_snapshot(user_id) -> dict:
    """Compact stats block for admin-facing approval cards — no need to open the full profile."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT grade, total_marks, total, correct, current_streak, personal_city, personal_country
                FROM user_stats WHERE user_id = %s;
            """, (str(user_id),))
            row = cur.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user snapshot: {e}", flush=True)
        return {}
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_set_show_real_identity(user_id, show: bool) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_stats (user_id, show_real_identity, total, correct, total_marks)
                VALUES (%s, %s, 0, 0, 0)
                ON CONFLICT (user_id) DO UPDATE SET show_real_identity = EXCLUDED.show_real_identity;
            """, (str(user_id), bool(show)))
            user_profile_cache.invalidate(f"profile:{user_id}")
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set show_real_identity: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_hide_question_for_user(user_id, q_id: str) -> bool:
    """Personal-only hide — never touches `questions` or `sent_tracks`. The question stays
    fully intact for everyone else; only this user's /myanswers list stops showing it."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_hidden_questions (user_id, q_id)
                VALUES (%s, %s)
                ON CONFLICT (user_id, q_id) DO NOTHING;
            """, (str(user_id), q_id))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to hide question for user: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_unhide_question_for_user(user_id, q_id: str) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_hidden_questions WHERE user_id = %s AND q_id = %s;", (str(user_id), q_id))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to unhide question for user: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_org_grade_breakdown(org_id: int):
    """Returns score summaries per grade for a specific school, including the school's rank
    for each grade on city, country, and world scales."""
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT city, country FROM organizations WHERE org_id = %s;", (int(org_id),))
            org = cur.fetchone()
            if not org:
                return []
            city, country = org.get('city'), org.get('country')

            cur.execute("""
                WITH school_grades AS (
                    SELECT u.grade, SUM(u.total_marks) AS school_grade_score
                    FROM user_stats u
                    JOIN org_memberships m ON u.user_id = m.user_id AND m.org_role NOT IN ('pending','rejected','left')
                    WHERE m.org_id = %s AND u.grade IS NOT NULL
                    GROUP BY u.grade
                ),
                world_grade_ranks AS (
                    SELECT o.org_id, u.grade, SUM(u.total_marks) AS score,
                           RANK() OVER (PARTITION BY u.grade ORDER BY SUM(u.total_marks) DESC) as world_rank
                    FROM organizations o
                    JOIN org_memberships m ON o.org_id = m.org_id AND m.org_role NOT IN ('pending','rejected','left')
                    JOIN user_stats u ON m.user_id = u.user_id
                    WHERE o.deleted_at IS NULL AND u.grade IS NOT NULL
                    GROUP BY o.org_id, u.grade
                ),
                country_grade_ranks AS (
                    SELECT o.org_id, u.grade, SUM(u.total_marks) AS score,
                           RANK() OVER (PARTITION BY u.grade ORDER BY SUM(u.total_marks) DESC) as country_rank
                    FROM organizations o
                    JOIN org_memberships m ON o.org_id = m.org_id AND m.org_role NOT IN ('pending','rejected','left')
                    JOIN user_stats u ON m.user_id = u.user_id
                    WHERE o.deleted_at IS NULL AND o.country = %s AND u.grade IS NOT NULL
                    GROUP BY o.org_id, u.grade
                ),
                city_grade_ranks AS (
                    SELECT o.org_id, u.grade, SUM(u.total_marks) AS score,
                           RANK() OVER (PARTITION BY u.grade ORDER BY SUM(u.total_marks) DESC) as city_rank
                    FROM organizations o
                    JOIN org_memberships m ON o.org_id = m.org_id AND m.org_role NOT IN ('pending','rejected','left')
                    JOIN user_stats u ON m.user_id = u.user_id
                    WHERE o.deleted_at IS NULL AND o.city = %s AND u.grade IS NOT NULL
                    GROUP BY o.org_id, u.grade
                )
                SELECT sg.grade, sg.school_grade_score,
                       wgr.world_rank,
                       coalesce(cgr.country_rank, 1) as country_rank,
                       coalesce(ctgr.city_rank, 1) as city_rank
                FROM school_grades sg
                LEFT JOIN world_grade_ranks wgr ON wgr.org_id = %s AND wgr.grade = sg.grade
                LEFT JOIN country_grade_ranks cgr ON cgr.org_id = %s AND cgr.grade = sg.grade
                LEFT JOIN city_grade_ranks ctgr ON ctgr.org_id = %s AND ctgr.grade = sg.grade
                ORDER BY sg.grade ASC;
            """, (int(org_id), country, city, int(org_id), int(org_id), int(org_id)))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch school grade breakdown: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_create_school_branch(org_id: int, branch_name: str, city: str, country: str) -> int:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO school_branches (org_id, branch_name, city, country)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (org_id, branch_name) DO UPDATE SET city = EXCLUDED.city, country = EXCLUDED.country
                RETURNING branch_id;
            """, (int(org_id), branch_name.strip(), city, country))
            row = cur.fetchone()
            conn.commit()
            return row['branch_id'] if row else None
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-BRANCH-ERROR] Create branch failed: {e}", flush=True)
        return None
    finally:
        if conn: GLOBAL_ENGINE.release_connection(conn)


def db_get_school_branches(org_id: int):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT b.*, COUNT(m.user_id) AS member_count
                FROM school_branches b
                LEFT JOIN org_memberships m ON m.branch_id = b.branch_id
                WHERE b.org_id = %s AND b.deleted_at IS NULL
                GROUP BY b.branch_id
                ORDER BY b.branch_name ASC;
            """, (int(org_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB-BRANCH-ERROR] Get branches failed: {e}", flush=True)
        return []
    finally:
        if conn: GLOBAL_ENGINE.release_connection(conn)


def db_join_branch(user_id, branch_id: int) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE org_memberships SET branch_id = %s
                WHERE user_id = %s
                  AND org_id = (SELECT org_id FROM school_branches WHERE branch_id = %s);
            """, (int(branch_id), str(user_id), int(branch_id)))
            cur.execute("UPDATE user_stats SET branch_id = %s WHERE user_id = %s;", (int(branch_id), str(user_id)))
            user_profile_cache.invalidate(f"profile:{user_id}")
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB-BRANCH-ERROR] Join branch failed: {e}", flush=True)
        return False
    finally:
        if conn: GLOBAL_ENGINE.release_connection(conn)




def db_get_team_geo_ownership(org_id: int) -> dict:
    """
    Determines whether a team's members are homogenous enough to be attributed
    to a specific school/branch/city/country for leaderboard purposes.
    Rules:
      - school: ALL active members belong to the same org_id
      - branch: ALL active members belong to the same branch_id
      - city: ALL members resolve to the same city (via branch > org > personal)
      - country: ALL members resolve to the same country
    Returns dict with keys: school_id, branch_id, city, country (None if mixed).
    """
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    m.user_id,
                    m.org_id,
                    m.branch_id,
                    COALESCE(b.city, o.city, u.personal_city) AS resolved_city,
                    COALESCE(b.country, o.country, u.personal_country) AS resolved_country
                FROM org_memberships m
                JOIN user_stats u ON u.user_id = m.user_id
                JOIN organizations o ON o.org_id = m.org_id
                LEFT JOIN school_branches b ON b.branch_id = m.branch_id
                WHERE m.org_id = %s
                  AND m.org_role NOT IN ('pending', 'rejected', 'left')
                  AND o.deleted_at IS NULL;
            """, (int(org_id),))
            rows = cur.fetchall()

        if not rows:
            return {"school_id": None, "branch_id": None, "city": None, "country": None}

        org_ids = set(r['org_id'] for r in rows)
        branch_ids = set(r['branch_id'] for r in rows if r['branch_id'])
        cities = set(r['resolved_city'] for r in rows if r['resolved_city'])
        countries = set(r['resolved_country'] for r in rows if r['resolved_country'])

        return {
            "school_id": int(list(org_ids)[0]) if len(org_ids) == 1 else None,
            "branch_id": int(list(branch_ids)[0]) if len(branch_ids) == 1 and len(rows) == len([r for r in rows if r['branch_id']]) else None,
            "city": list(cities)[0] if len(cities) == 1 else None,
            "country": list(countries)[0] if len(countries) == 1 else None,
        }
    except Exception as e:
        print(f"[DB ERROR] db_get_team_geo_ownership: {e}", flush=True)
        return {"school_id": None, "branch_id": None, "city": None, "country": None}
    finally:
        if conn: GLOBAL_ENGINE.release_connection(conn)


def db_get_branch_leaderboard(branch_id: int, limit: int = 10):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.user_id, u.nickname, u.username, u.first_name,
                       u.public_consent_granted, u.total_marks
                FROM user_stats u
                WHERE u.branch_id = %s
                ORDER BY u.total_marks DESC
                LIMIT %s;
            """, (int(branch_id), limit))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] db_get_branch_leaderboard: {e}", flush=True)
        return []
    finally:
        if conn: GLOBAL_ENGINE.release_connection(conn)


def db_get_school_with_branches_leaderboard(limit: int = 10):
    """
    Returns schools ranked by total marks, including branch breakdowns.
    Each row has: org_id, org_name, total_score, branches (list).
    """
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            # School totals
            cur.execute("""
                SELECT o.org_id, o.org_name, o.org_tag, o.city, o.country,
                       SUM(u.total_marks) AS total_score,
                       COUNT(DISTINCT m.user_id) AS member_count
                FROM organizations o
                JOIN org_memberships m ON o.org_id = m.org_id
                  AND m.org_role NOT IN ('pending','rejected','left')
                JOIN user_stats u ON m.user_id = u.user_id
                WHERE o.deleted_at IS NULL
                GROUP BY o.org_id, o.org_name, o.org_tag, o.city, o.country
                ORDER BY total_score DESC
                LIMIT %s;
            """, (limit,))
            schools = [dict(r) for r in cur.fetchall()]

            # Branch totals per school
            for s in schools:
                cur.execute("""
                    SELECT b.branch_id, b.branch_name, b.city, b.country,
                           SUM(u.total_marks) AS branch_score,
                           COUNT(DISTINCT m.user_id) AS member_count
                    FROM school_branches b
                    JOIN org_memberships m ON m.branch_id = b.branch_id
                      AND m.org_role NOT IN ('pending','rejected','left')
                    JOIN user_stats u ON m.user_id = u.user_id
                    WHERE b.org_id = %s AND b.deleted_at IS NULL
                    GROUP BY b.branch_id, b.branch_name, b.city, b.country
                    ORDER BY branch_score DESC;
                """, (s['org_id'],))
                s['branches'] = [dict(r) for r in cur.fetchall()]

            return schools
    except Exception as e:
        print(f"[DB ERROR] db_get_school_with_branches_leaderboard: {e}", flush=True)
        return []
    finally:
        if conn: GLOBAL_ENGINE.release_connection(conn)


def db_get_smart_team_leaderboard(scope: str = "world", scope_value: str = None, limit: int = 10):
    """
    scope: 'world' | 'country' | 'city' | 'school'
    Only includes teams where ALL members share the same scope_value.
    Uses db_get_team_geo_ownership logic inline via SQL.
    """
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            # Subquery: for each org, get all unique resolved countries and cities
            base_sql = """
                WITH team_geo AS (
                    SELECT
                        m.org_id,
                        COUNT(DISTINCT m.user_id) AS member_count,
                        COUNT(DISTINCT COALESCE(b.country, o.country, u.personal_country)) AS country_count,
                        COUNT(DISTINCT COALESCE(b.city, o.city, u.personal_city)) AS city_count,
                        MIN(COALESCE(b.country, o.country, u.personal_country)) AS solo_country,
                        MIN(COALESCE(b.city, o.city, u.personal_city)) AS solo_city
                    FROM org_memberships m
                    JOIN user_stats u ON u.user_id = m.user_id
                    JOIN organizations o ON o.org_id = m.org_id
                    LEFT JOIN school_branches b ON b.branch_id = m.branch_id
                    WHERE m.org_role NOT IN ('pending','rejected','left')
                      AND o.deleted_at IS NULL
                    GROUP BY m.org_id
                ),
                ranked_teams AS (
                    SELECT
                        tg.org_id,
                        o.org_name,
                        o.org_tag,
                        SUM(u.total_marks) AS total_score,
                        tg.member_count,
                        tg.solo_country,
                        tg.solo_city,
                        tg.country_count,
                        tg.city_count
                    FROM team_geo tg
                    JOIN organizations o ON o.org_id = tg.org_id
                    JOIN org_memberships m ON m.org_id = tg.org_id
                      AND m.org_role NOT IN ('pending','rejected','left')
                    JOIN user_stats u ON u.user_id = m.user_id
                    GROUP BY tg.org_id, o.org_name, o.org_tag,
                             tg.member_count, tg.solo_country, tg.solo_city,
                             tg.country_count, tg.city_count
                )
            """

            if scope == "country" and scope_value:
                cur.execute(base_sql + """
                    SELECT * FROM ranked_teams
                    WHERE country_count = 1 AND solo_country = %s
                    ORDER BY total_score DESC LIMIT %s;
                """, (scope_value, limit))
            elif scope == "city" and scope_value:
                cur.execute(base_sql + """
                    SELECT * FROM ranked_teams
                    WHERE city_count = 1 AND solo_city = %s
                    ORDER BY total_score DESC LIMIT %s;
                """, (scope_value, limit))
            elif scope == "school" and scope_value:
                cur.execute(base_sql + """
                    SELECT rt.* FROM ranked_teams rt
                    JOIN team_geo tg ON tg.org_id = rt.org_id
                    WHERE rt.org_id = %s
                    ORDER BY rt.total_score DESC LIMIT %s;
                """, (int(scope_value), limit))
            else:
                # World — all teams qualify
                cur.execute(base_sql + """
                    SELECT * FROM ranked_teams
                    ORDER BY total_score DESC LIMIT %s;
                """, (limit,))

            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] db_get_smart_team_leaderboard({scope}): {e}", flush=True)
        return []
    finally:
        if conn: GLOBAL_ENGINE.release_connection(conn)


def db_get_user_favorites(user_id, fav_type):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fav_value, fav_label FROM user_favorites
                WHERE user_id = %s AND fav_type = %s ORDER BY created_at DESC LIMIT 12;
            """, (str(user_id), fav_type))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] db_get_user_favorites: {e}", flush=True)
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_add_favorite(user_id, fav_type, fav_value, fav_label) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_favorites (user_id, fav_type, fav_value, fav_label)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, fav_type, fav_value) DO NOTHING;
            """, (str(user_id), fav_type, fav_value, fav_label))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] db_add_favorite: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)


def db_remove_favorite(user_id, fav_type, fav_value) -> bool:
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_favorites WHERE user_id = %s AND fav_type = %s AND fav_value = %s;",
                        (str(user_id), fav_type, fav_value))
            conn.commit()
            return True
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] db_remove_favorite: {e}", flush=True)
        return False
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)