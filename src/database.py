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
from src.config import CONFIG, Style

class QuizEngine:
    _pool = None
    _warned_detected = False
    _tracks_cache = {}
    _tracks_cache_time = 0

    def __init__(self):
        self.config = CONFIG
        self.db = {}
        self.last_refresh = 0
        self.refresh_interval = 30  # 30 seconds caching TTL
        self.db_url = self.config.get("database_url")
        if self.db_url:
            if not QuizEngine._pool:
                try:
                    QuizEngine._pool = ThreadedConnectionPool(
                        minconn=2,
                        maxconn=20,
                        dsn=self.db_url
                    )
                    if not QuizEngine._warned_detected:
                        print(f"{Style.GREEN}[DATABASE] Threaded PostgreSQL Connection Pool initialized.{Style.RESET}")
                        QuizEngine._warned_detected = True
                except Exception as e:
                    print(f"{Style.RED}[DATABASE ERROR] Failed to initialize connection pool: {e}{Style.RESET}")
        else:
            print(f"{Style.YELLOW}[DATABASE] Running without cloud database environment.{Style.RESET}")

    def get_db_connection(self):
        if not self.db_url:
            raise ConnectionError("DATABASE_URL environment variable is missing.")

        if QuizEngine._pool:
            for _ in range(3):
                try:
                    conn = QuizEngine._pool.getconn()
                    conn.cursor_factory = RealDictCursor

                    if conn.closed == 0:
                        try:
                            with conn.cursor() as cur:
                                cur.execute("SELECT 1;")
                            return conn
                        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                            print(f"{Style.YELLOW}[DATABASE] Discarding stale connection from pool: {e}{Style.RESET}")
                            try:
                                QuizEngine._pool.putconn(conn, close=True)
                            except Exception:
                                pass
                    else:
                        try:
                            QuizEngine._pool.putconn(conn, close=True)
                        except Exception:
                            pass
                except Exception as e:
                    print(f"{Style.YELLOW}[DATABASE WARNING] Connection pool checkout failed: {e}{Style.RESET}")
                    break

        return psycopg2.connect(
            self.db_url,
            cursor_factory=RealDictCursor
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

    # --- TRACKING STATE METHODS ---
    def db_save_track(self, message_id, q_id, status, display_id, type_, msg_type, followup_mid=None):
        QuizEngine._tracks_cache_time = 0
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, followup_mid)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (message_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        followup_mid = EXCLUDED.followup_mid;
                """, (str(message_id), q_id, status, int(display_id), type_, msg_type, followup_mid))
                conn.commit()
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to save track: {e}{Style.RESET}")
        finally:
            if conn:
                self.release_connection(conn)

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
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to update track status: {e}{Style.RESET}")
        finally:
            if conn:
                self.release_connection(conn)

    def db_swap_track_message_id(self, old_mid, new_mid):
        """Swaps the message ID inside both track records and user responses for deleted photo elements."""
        QuizEngine._tracks_cache_time = 0
        conn = None
        try:
            conn = self.get_db_connection()
            with conn.cursor() as cur:
                try:
                    cur.execute("UPDATE user_responses SET message_id = %s WHERE message_id = %s;", (str(new_mid), str(old_mid)))
                except Exception as e:
                    print(f"[DB SWAP WARNING] user_responses update bypassed: {e}")
                
                cur.execute("UPDATE sent_tracks SET message_id = %s, msg_type = 'text' WHERE message_id = %s;", (str(new_mid), str(old_mid)))
                conn.commit()
        except Exception as e:
            if conn: conn.rollback()
            print(f"[DB ERROR] Failed to swap track message ID: {e}")
        finally:
            if conn:
                self.release_connection(conn)

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

                    tags = q.get("tags", [])
                    options = q.get("options", [])
                    poll_explanation = Json(q.get("poll_explanation", {}))
                    options_analysis = Json(q.get("options_analysis", []))
                    scheduled_for = q.get("scheduled_for")
                    force_image = q.get("force_image", False)
                    native_question = q.get("native_question")
                    native_options = q.get("native_options")

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
                        for field in ["poll_explanation", "options_analysis", "tags", "options", "native_options"]:
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

def db_get_responses_for_message(message_id: str):
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT user_id, private_message_id, selected_option, is_correct
                FROM user_responses
                WHERE message_id = %s;
            """, (str(message_id),))
            return cur.fetchall()
    except Exception as e:
        print(f"[DB ERROR] Failed to fetch responses for message {message_id}: {e}")
        return []
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)

def db_get_track_and_question(display_id: int):
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

            for field in ["poll_explanation", "options_analysis", "tags", "options", "native_options"]:
                if field in row_dict and isinstance(row_dict[field], str):
                    try:
                        row_dict[field] = json.loads(row_dict[field])
                    except Exception:
                        pass
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
                SELECT ur.user_id, SUM(ur.marks_awarded) as total_score
                FROM user_responses ur
                JOIN user_stats us ON ur.user_id = us.user_id
                WHERE us.grade = %s
                  AND ur.answered_at >= NOW() - INTERVAL '7 days'
                GROUP BY ur.user_id
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
    conn = None
    try:
        conn = GLOBAL_ENGINE.get_db_connection()
        with conn.cursor() as cur:
            first_try = True
            marks_to_award = 0
            is_bonus_winner = False

            cur.execute("""
                SELECT is_correct, marks_awarded FROM user_responses
                WHERE user_id = %s AND message_id = %s;
            """, (str(user_id), str(message_id)))
            existing_response = cur.fetchone()

            if existing_response:
                first_try = False
                marks_to_award = existing_response['marks_awarded']
                is_bonus_winner = (marks_to_award >= 10)
                cur.execute("SELECT total, correct, total_marks, grade, current_streak FROM user_stats WHERE user_id = %s;", (str(user_id),))
                stats = cur.fetchone()
            else:
                streak_multiplier = 1.0
                cur.execute("SELECT last_active_at, current_streak FROM user_stats WHERE user_id = %s;", (str(user_id),))
                user_meta = cur.fetchone()

                current_streak = 0
                today = datetime.now(timezone.utc).date()

                if user_meta:
                    last_date = user_meta.get('last_active_at')
                    current_streak = user_meta.get('current_streak', 0) or 0

                    if last_date:
                        if isinstance(last_date, (datetime, date)):
                            last_active_date = last_date.date() if isinstance(last_date, datetime) else last_date
                        elif isinstance(last_date, str):
                            try:
                                last_active_date = datetime.fromisoformat(last_date.replace('Z', '+00:00')).date()
                            except Exception:
                                last_active_date = today
                        else:
                            last_active_date = today

                        days_diff = (today - last_active_date).days
                        if days_diff == 1:
                            current_streak += 1
                        elif days_diff > 1:
                            current_streak = 1
                    else:
                        current_streak = 1
                else:
                    current_streak = 1

                if current_streak >= 7:
                    streak_multiplier = 1.5
                elif current_streak >= 3:
                    streak_multiplier = 1.2

                if is_correct:
                    cur.execute("""
                        SELECT COUNT(*) FROM user_responses
                        WHERE message_id = %s AND is_correct = TRUE;
                    """, (str(message_id),))
                    correct_count = cur.fetchone()['count']

                    if correct_count < bonus_limit:
                        base_marks = 10
                        is_bonus_winner = True
                    else:
                        base_marks = 2

                    marks_to_award = int(base_marks * streak_multiplier)
                else:
                    marks_to_award = 0

                try:
                    cur.execute("SAVEPOINT score_insertion_sp;")

                    cur.execute("""
                        INSERT INTO user_responses (user_id, message_id, q_id, is_correct, marks_awarded, selected_option, private_message_id, show_derivation, show_perf)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
                    """, (str(user_id), str(message_id), q_id, is_correct, marks_to_award, int(selected_option), private_message_id, show_derivation, show_perf))

                    correct_inc = 1 if is_correct else 0

                    cur.execute("""
                        INSERT INTO user_stats (user_id, total, correct, total_marks, current_streak, last_active_at)
                        VALUES (%s, 1, %s, %s, %s, NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            total = COALESCE(user_stats.total, 0) + 1,
                            correct = COALESCE(user_stats.correct, 0) + %s,
                            total_marks = COALESCE(user_stats.total_marks, 0) + %s,
                            current_streak = %s,
                            last_active_at = NOW();
                    """, (str(user_id), correct_inc, marks_to_award, current_streak, correct_inc, marks_to_award, current_streak))

                    cur.execute("RELEASE SAVEPOINT score_insertion_sp;")
                except psycopg2.IntegrityError:
                    cur.execute("ROLLBACK TO SAVEPOINT score_insertion_sp;")
                    cur.execute("""
                        SELECT is_correct, marks_awarded FROM user_responses
                        WHERE user_id = %s AND message_id = %s;
                    """, (str(user_id), str(message_id)))
                    existing_response_retry = cur.fetchone()
                    if existing_response_retry:
                        first_try = False
                        marks_to_award = existing_response_retry['marks_awarded']
                        is_bonus_winner = (marks_to_award >= 10)
                    else:
                        first_try = False
                        marks_to_award = 0
                        is_bonus_winner = False

                conn.commit()

                cur.execute("SELECT total, correct, total_marks, grade, current_streak FROM user_stats WHERE user_id = %s;", (str(user_id),))
                stats = cur.fetchone()

        if stats:
            accuracy = int((stats['correct'] / stats['total']) * 100) if stats['total'] > 0 else 0
            return {
                "total": stats['total'],
                "correct": stats['correct'],
                "accuracy": accuracy,
                "total_marks": stats['total_marks'],
                "marks_awarded": marks_to_award,
                "first_try": first_try,
                "is_bonus_winner": is_bonus_winner,
                "grade": stats['grade'],
                "current_streak": stats.get('current_streak', 0)
            }
        return None
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Error in process_user_score: {e}")
        return None
    finally:
        if conn:
            GLOBAL_ENGINE.release_connection(conn)