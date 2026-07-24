# src/database.py
import os
import json
import time
import psycopg2
import psycopg2.extensions
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
from pathlib import Path
from src.config import CONFIG, Style

class PooledConnection(psycopg2.extensions.connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._is_pooled = False
        self._closed_in_pool = False

    def close(self):
        """Overrides close to return the connection to the pool if pooled."""
        if hasattr(self, "_is_pooled") and self._is_pooled:
            if hasattr(self, "_closed_in_pool") and self._closed_in_pool:
                return
            try:
                self.rollback()  # Safely roll back transaction state before releasing
                if QuizEngine._pool:
                    QuizEngine._pool.putconn(self)
                self._closed_in_pool = True
            except Exception:
                super().close()
        else:
            super().close()

class QuizEngine:
    _pool = None

    def __init__(self):
        self.config = CONFIG
        self.db = {}
        self.last_refresh = 0
        self.refresh_interval = 30  # 30 seconds caching TTL
        self.db_url = self.config.get("database_url")
        if self.db_url:
            print(f"{Style.GREEN}[DATABASE] PostgreSQL detected in environment.{Style.RESET}")
            if not QuizEngine._pool:
                try:
                    QuizEngine._pool = ThreadedConnectionPool(
                        minconn=2,
                        maxconn=20,
                        dsn=self.db_url,
                        connection_factory=PooledConnection
                    )
                    print(f"{Style.GREEN}[DATABASE] Threaded PostgreSQL Connection Pool initialized.{Style.RESET}")
                except Exception as e:
                    print(f"{Style.RED}[DATABASE ERROR] Failed to initialize connection pool: {e}{Style.RESET}")
        else:
            print(f"{Style.YELLOW}[DATABASE] Running without cloud database environment.{Style.RESET}")

    def get_db_connection(self):
        """Retrieves a pooled connection or falls back to a direct connection."""
        if not self.db_url:
            raise ConnectionError("DATABASE_URL environment variable is missing.")
        
        if QuizEngine._pool:
            try:
                conn = QuizEngine._pool.getconn()
                conn.cursor_factory = RealDictCursor
                conn._is_pooled = True
                conn._closed_in_pool = False
                return conn
            except Exception as e:
                print(f"{Style.YELLOW}[DATABASE WARNING] Connection pool getconn failed, falling back to direct connection: {e}{Style.RESET}")
                
        # Direct connection fallback
        return psycopg2.connect(
            self.db_url, 
            cursor_factory=RealDictCursor, 
            connection_factory=PooledConnection
        )

    # --- TRACKING STATE METHODS ---
    def db_save_track(self, message_id, q_id, status, display_id, type_, msg_type, followup_mid=None):
        conn = None
        try:
            conn = self.get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, followup_mid)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (message_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    followup_mid = EXCLUDED.followup_mid;
            """, (str(message_id), q_id, status, int(display_id), type_, msg_type, followup_mid))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to save track: {e}{Style.RESET}")

    def db_get_all_tracks(self):
        conn = None
        try:
            conn = self.get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT * FROM sent_tracks;")
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return {r['message_id']: dict(r) for r in rows}
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to retrieve tracks: {e}{Style.RESET}")
            return {}

    def db_update_track_status(self, message_id, status, followup_mid=None):
        conn = None
        try:
            conn = self.get_db_connection()
            cur = conn.cursor()
            if followup_mid is not None:
                cur.execute("UPDATE sent_tracks SET status = %s, followup_mid = %s WHERE message_id = %s;", (status, followup_mid, str(message_id)))
            else:
                cur.execute("UPDATE sent_tracks SET status = %s WHERE message_id = %s;", (status, str(message_id)))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to update track status: {e}{Style.RESET}")

    # --- AI QUESTIONS DYNAMIC DATABASE IMPORTER ---
    def db_import_questions(self, json_data):
        """Imports questions dynamically into the PostgreSQL questions table."""
        conn = None
        try:
            questions_list = json_data if isinstance(json_data, list) else [json_data]
            conn = self.get_db_connection()
            cur = conn.cursor()
            imported_count = 0

            for q in questions_list:
                if not q.get("id") or not q.get("subject"):
                    continue

                tags = q.get("tags", [])
                options = q.get("options", [])
                poll_explanation = json.dumps(q.get("poll_explanation", {}))
                options_analysis = json.dumps(q.get("options_analysis", []))

                cur.execute("""
                    INSERT INTO questions (id, subject, topic, difficulty, tags, question, latex, options, correct_option, poll_explanation, options_analysis)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                        options_analysis = EXCLUDED.options_analysis;
                """, (
                    q["id"], q["subject"], q["topic"], q.get("difficulty", "medium"),
                    tags, q["question"], q.get("latex"), options, int(q["correct_option"]),
                    poll_explanation, options_analysis
                ))
                imported_count += 1

            conn.commit()
            cur.close()
            conn.close()
            return imported_count
        except Exception as e:
            if conn: conn.rollback()
            print(f"{Style.RED}[DB ERROR] Failed to import questions: {e}{Style.RESET}")
            return 0

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
        """Loads and syncs questions with a TTL caching mechanism."""
        now = time.time()
        if self.db and not force and (now - self.last_refresh < self.refresh_interval):
            return self.db

        self.db = {}
        self.last_refresh = now
        
        if self.db_url:
            try:
                conn = self.get_db_connection()
                cur = conn.cursor()
                cur.execute("SELECT * FROM questions;")
                rows = cur.fetchall()
                cur.close()
                conn.close()

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

        return self.refresh_database_local()

    def refresh_database_local(self):
        self.db = {}
        questions_dir = Path("questions")
        if not questions_dir.exists():
            questions_dir.mkdir()

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

# --- COMPREHENSIVE OUT-OF-CLASS DB UTILITIES ---
def db_set_user_grade(user_id, grade: int):
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_stats (user_id, grade)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO UPDATE SET grade = EXCLUDED.grade;
        """, (str(user_id), int(grade)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to set user grade: {e}")
    finally:
        if conn: conn.close()

def db_get_user_profile(user_id):
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_stats WHERE user_id = %s;", (str(user_id),))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to fetch user profile: {e}")
        return None
    finally:
        if conn: conn.close()

def db_get_user_response(user_id, message_id):
    """Retrieves a user's previous response to a specific question post."""
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM user_responses
            WHERE user_id = %s AND message_id = %s;
        """, (str(user_id), str(message_id)))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to fetch user response: {e}")
        return None
    finally:
        if conn: conn.close()

def db_update_private_message_id(user_id, message_id, private_message_id):
    """Updates the private message ID of an existing response."""
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE user_responses
            SET private_message_id = %s
            WHERE user_id = %s AND message_id = %s;
        """, (int(private_message_id), str(user_id), str(message_id)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update private message ID: {e}")
    finally:
        if conn: conn.close()

def db_update_response_view_state(user_id, message_id, show_derivation: bool, show_perf: bool):
    """Updates the saved view layout toggles inside the user_responses table."""
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE user_responses
            SET show_derivation = %s, show_perf = %s
            WHERE user_id = %s AND message_id = %s;
        """, (show_derivation, show_perf, str(user_id), str(message_id)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to update response view state: {e}")
    finally:
        if conn: conn.close()

def db_get_weekly_leaderboard(grade: int):
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
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
        cur.close()
        conn.close()
        return rows
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to fetch weekly leaderboard: {e}")
        return []
    finally:
        if conn: conn.close()

def db_get_pending_scheduled_question():
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM questions
            WHERE is_sent = FALSE
              AND scheduled_for IS NOT NULL
              AND scheduled_for <= NOW()
            ORDER BY scheduled_for ASC
            LIMIT 1;
        """)
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to fetch scheduled question: {e}")
        return None
    finally:
        if conn: conn.close()

def db_mark_question_as_sent(q_id):
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()
        cur.execute("UPDATE questions SET is_sent = TRUE WHERE id = %s;", (q_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Failed to mark question as sent: {e}")
    finally:
        if conn: conn.close()

def process_user_score(user_id, message_id, q_id, is_correct, selected_option, private_message_id=None, show_derivation=False, show_perf=False, bonus_limit=3):
    engine_db = QuizEngine()
    conn = None
    try:
        conn = engine_db.get_db_connection()
        cur = conn.cursor()

        first_try = True
        marks_to_award = 0
        is_bonus_winner = False

        cur.execute("""
            SELECT EXISTS(SELECT 1 FROM user_responses WHERE user_id = %s AND message_id = %s);
        """, (str(user_id), str(message_id)))
        already_answered = cur.fetchone()['exists']

        if already_answered:
            first_try = False
        else:
            if is_correct:
                cur.execute("""
                    SELECT COUNT(*) FROM user_responses
                    WHERE message_id = %s AND is_correct = TRUE;
                """, (str(message_id),))
                correct_count = cur.fetchone()['count']

                if correct_count < bonus_limit:
                    marks_to_award = 10
                    is_bonus_winner = True
                else:
                    marks_to_award = 2
            else:
                marks_to_award = 0

            # Store expansion states when inserting the initial response record
            cur.execute("""
                INSERT INTO user_responses (user_id, message_id, q_id, is_correct, marks_awarded, selected_option, private_message_id, show_derivation, show_perf)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, (str(user_id), str(message_id), q_id, is_correct, marks_to_award, int(selected_option), private_message_id, show_derivation, show_perf))

            correct_inc = 1 if is_correct else 0
            cur.execute("""
                INSERT INTO user_stats (user_id, total, correct, total_marks)
                VALUES (%s, 1, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    total = user_stats.total + 1,
                    correct = user_stats.correct + %s,
                    total_marks = user_stats.total_marks + %s;
            """, (str(user_id), correct_inc, marks_to_award, correct_inc, marks_to_award))
            conn.commit()

        cur.execute("SELECT total, correct, total_marks, grade FROM user_stats WHERE user_id = %s;", (str(user_id),))
        stats = cur.fetchone()

        cur.close()
        conn.close()

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
                "grade": stats['grade']
            }
        return None
    except Exception as e:
        if conn: conn.rollback()
        print(f"[DB ERROR] Error in process_user_score: {e}")
        return None
    finally:
        if conn: conn.close()