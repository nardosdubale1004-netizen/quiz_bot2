# src/database.py
import os
import json
import time
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

_FN_PROCESS_USER_SCORE_SQL = """
CREATE OR REPLACE FUNCTION fn_process_user_score(
    p_user_id text,
    p_message_id text,
    p_q_id text,
    p_is_correct boolean,
    p_selected_option int,
    p_private_message_id bigint, -- Aligned to match the bigint schema column
    p_show_derivation boolean,
    p_show_perf boolean,
    p_bonus_limit int
) RETURNS TABLE (
    o_total int,
    o_correct int,
    o_total_marks int,
    o_marks_awarded int,
    o_first_try boolean,
    o_is_bonus_winner boolean,
    o_grade int,
    o_current_streak int
) AS $$
DECLARE
    v_existing_correct boolean;
    v_existing_marks int;
    v_found boolean := false;
    v_marks int := 0;
    v_first_try boolean := true;
    v_is_bonus boolean := false;
    v_streak int := 0;
    v_streak_mult numeric := 1.0;
    v_last_active timestamptz;
    v_last_active_date date;
    v_today date := (NOW() AT TIME ZONE 'utc')::date;
    v_days_diff int;
    v_correct_count int;
    v_base_marks int;
BEGIN
    SELECT ur.is_correct, ur.marks_awarded
      INTO v_existing_correct, v_existing_marks
      FROM user_responses ur
     WHERE ur.user_id = p_user_id AND ur.message_id = p_message_id;

    IF FOUND THEN
        v_first_try := false;
        v_marks := v_existing_marks;
        v_is_bonus := (v_marks >= 10);
    ELSE
        SELECT us.last_active_at, COALESCE(us.current_streak, 0)
          INTO v_last_active, v_streak
          FROM user_stats us
         WHERE us.user_id = p_user_id;

        IF v_last_active IS NULL THEN
            v_streak := 1;
        ELSE
            v_last_active_date := v_last_active::date;
            v_days_diff := v_today - v_last_active_date;
            if v_days_diff = 1 THEN
                v_streak := v_streak + 1;
            ELSIF v_days_diff > 1 THEN
                v_streak := 1;
            END IF;
        END IF;

        IF v_streak >= 7 THEN
            v_streak_mult := 1.5;
        ELSIF v_streak >= 3 THEN
            v_streak_mult := 1.2;
        END IF;

        IF p_is_correct THEN
            SELECT COUNT(*) INTO v_correct_count
              FROM user_responses
             WHERE message_id = p_message_id AND is_correct = TRUE;

            IF v_correct_count < p_bonus_limit THEN
                v_base_marks := 10;
                v_is_bonus := true;
            ELSE
                v_base_marks := 2;
            END IF;
            v_marks := FLOOR(v_base_marks * v_streak_mult)::int;
        ELSE
            v_marks := 0;
        END IF;

        BEGIN
            -- Insert the stats record first to avoid foreign key errors on user_responses.user_id references user_stats(user_id)
            INSERT INTO user_stats (user_id, total, correct, total_marks, current_streak, last_active_at)
            VALUES (
                p_user_id, 1,
                CASE WHEN p_is_correct THEN 1 ELSE 0 END,
                v_marks, v_streak, NOW()
            )
            ON CONFLICT (user_id) DO UPDATE SET
                total = COALESCE(user_stats.total, 0) + 1,
                correct = COALESCE(user_stats.correct, 0) + CASE WHEN p_is_correct THEN 1 ELSE 0 END,
                total_marks = COALESCE(user_stats.total_marks, 0) + v_marks,
                current_streak = v_streak,
                last_active_at = NOW();

            INSERT INTO user_responses (
                user_id, message_id, q_id, is_correct, marks_awarded,
                selected_option, private_message_id, show_derivation, show_perf
            )
            VALUES (
                p_user_id, p_message_id, p_q_id, p_is_correct, v_marks,
                p_selected_option, p_private_message_id, p_show_derivation, p_show_perf
            );

        EXCEPTION WHEN unique_violation THEN
            v_first_try := false;
            SELECT ur.is_correct, ur.marks_awarded
              INTO v_existing_correct, v_existing_marks
              FROM user_responses ur
             WHERE ur.user_id = p_user_id AND ur.message_id = p_message_id;
            IF FOUND THEN
                v_marks := v_existing_marks;
                v_is_bonus := (v_marks >= 10);
            ELSE
                v_marks := 0;
                v_is_bonus := false;
            END If;
        END;
    END IF;

    RETURN QUERY
    SELECT us.total, us.correct, us.total_marks, v_marks, v_first_try, v_is_bonus, us.grade, us.current_streak
      FROM user_stats us
     WHERE us.user_id = p_user_id;
END;
$$ LANGUAGE plpgsql;
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
                cur.execute(_FN_PROCESS_USER_SCORE_SQL)
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
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM sent_tracks LIMIT 1;")
                cur.execute("SELECT 1 FROM tournament_queue LIMIT 1;")

                # Self-healing Schema Updates to track nicknames and Telegram attributes
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS username text;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS first_name text;")
                cur.execute("ALTER TABLE user_stats ADD COLUMN IF NOT EXISTS nickname text;")

                # Clear restrictive foreign key constraint to permit message ID swaps without violations
                cur.execute("ALTER TABLE user_responses DROP CONSTRAINT IF EXISTS user_responses_message_id_fkey;")
                conn.commit()

            QuizEngine._tournament_schema_ensured = True
            print(f"{Style.GREEN}[DATABASE] Production schema configuration & fallback username columns verified.{Style.RESET}")
            print(f"{Style.GREEN}[DEBUG-SCHEMA-FIX] Successfully dropped restrictive FK constraint user_responses_message_id_fkey.{Style.RESET}", flush=True)
        except Exception as e:
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

        return psycopg2.connect(
            self.db_url,
            cursor_factory=RealDictCursor,
            connect_timeout=5
        )

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
            conn = self.get_db_connection()
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
        try:
            questions_list = json_data if isinstance(json_data, list) else [json_data]
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                imported_count = 0

                for q in questions_list:
                    if not q.get("id") or not q.get("subject"):
                        continue

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
                SELECT alliance_tag, SUM(total_marks) as total_score, COUNT(user_id) as active_members
                FROM user_stats
                WHERE alliance_tag IS NOT NULL
                GROUP BY alliance_tag
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
                           us.alliance_tag, us.nickname, us.username, us.first_name
                    FROM user_responses ur
                    LEFT JOIN user_stats us ON ur.user_id = us.user_id
                    WHERE ur.message_id = %s OR ur.message_id = %s
                    ORDER BY ur.answered_at ASC;
                """, (str(message_id), placeholder_id))
            else:
                print(f"[DEBUG-DB-GET-RESPONSES] Querying user_responses for message_id={message_id}", flush=True)
                cur.execute("""
                    SELECT ur.user_id, ur.private_message_id, ur.selected_option, ur.is_correct, ur.answered_at,
                           us.alliance_tag, us.nickname, us.username, us.first_name
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

def db_save_tournament_queue(remaining_ids: list, last_seq: int, round_seconds: int = 60, total_count: int = 1, scheduled_start=None, announcement_mid=None, cooldown_seconds: int = 15):
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

            cur.execute("""
                INSERT INTO tournament_queue (id, remaining_ids, last_seq, round_seconds, total_count, scheduled_start, announcement_mid, cooldown_seconds)
                VALUES (1, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    remaining_ids = EXCLUDED.remaining_ids,
                    last_seq = EXCLUDED.last_seq,
                    round_seconds = EXCLUDED.round_seconds,
                    total_count = EXCLUDED.total_count,
                    scheduled_start = EXCLUDED.scheduled_start,
                    announcement_mid = EXCLUDED.announcement_mid,
                    cooldown_seconds = EXCLUDED.cooldown_seconds;
            """, (Json(remaining_ids), last_seq, round_seconds, total_count, scheduled_start, announcement_mid, cooldown_seconds))
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

def db_pop_tournament_question():
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT remaining_ids, last_seq FROM tournament_queue WHERE id = 1 FOR UPDATE;")
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None, None

            remaining = row['remaining_ids']
            if isinstance(remaining, str):
                try:
                    remaining = json.loads(remaining)
                except Exception:
                    remaining = []

            if not remaining:
                print("[DEBUG-DB-POP] Remaining ids is empty. Aborting pop.", flush=True)
                conn.commit()
                return None, None

            cur.execute("SELECT 1 FROM sent_tracks WHERE status = 'tournament_active' LIMIT 1;")
            if cur.fetchone():
                print("[DEBUG-DB-POP] Active live round already detected in tracks table. Bypassing popping.", flush=True)
                conn.commit()
                return None, None

            next_id = remaining.pop(0)
            new_last_seq = row['last_seq'] + 1

            print(f"[DEBUG-DB-POP] Popped next tournament question: '{next_id}'. New remaining queue: {remaining}", flush=True)
            cur.execute(
                "UPDATE tournament_queue SET remaining_ids = %s, last_seq = %s WHERE id = 1;",
                (Json(remaining), new_last_seq)
            )
            conn.commit()
            return next_id, new_last_seq
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to pop tournament question: {e}")
        return None, None
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
            conn.commit()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set user grade: {e}")
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_user_profile(user_id):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM user_stats WHERE user_id = %s;", (str(user_id),))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch user profile: {e}")
        return None
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
                       us.nickname, us.username, us.first_name, us.alliance_tag
                FROM user_responses ur
                JOIN user_stats us ON ur.user_id = us.user_id
                WHERE us.grade = %s
                  AND ur.answered_at >= NOW() - INTERVAL '7 days'
                GROUP BY ur.user_id, us.nickname, us.username, us.first_name, us.alliance_tag
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

def process_user_score(user_id, message_id, q_id, is_correct, selected_option, private_message_id=None, show_derivation=False, show_perf=False, bonus_limit=3):
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
                    "SELECT * FROM fn_process_user_score(%s, %s, %s, %s, %s, %s, %s, %s, %s);",
                    (
                        str(user_id), str(message_id), q_id, bool(is_correct), int(selected_option),
                        pm_id, bool(show_derivation), bool(show_perf), int(bonus_limit)
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
                "is_bonus_winner": row['o_is_bonus_winner'],
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
                f"user={user_id}, message_id={message_id}, q_id={q_id})",
                e
            )
            if attempt < max_attempts:
                dlog(f"[DEBUG-DB-SCORE] Retrying process_user_score for user={user_id}, "
                     f"message_id={message_id} after transient failure...")
                time.sleep(0.5)
                continue
        finally:
            if conn:
                GLOBAL_ENGINE.release_connection(conn)

    dlog(f"[DEBUG-DB-SCORE] All {max_attempts} attempts failed for user={user_id}, "
         f"message_id={message_id}. Raising last exception to caller.")
    raise last_exc

def db_try_start_tournament_round(message_id, q_id, display_id, round_seconds, round_number, total_rounds):
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
                    (message_id, q_id, status, display_id, type, msg_type, round_deadline, round_number, total_rounds)
                VALUES
                    (%s, %s, 'tournament_active', %s, 'premium', 'text',
                     NOW() + (%s || ' second')::interval, %s, %s)
                ON CONFLICT (message_id) DO NOTHING;
            """, (str(message_id), q_id, int(display_id), int(round_seconds), int(round_number), int(total_rounds)))
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