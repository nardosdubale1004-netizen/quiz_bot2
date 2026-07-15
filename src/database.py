import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
from pathlib import Path
from src.config import CONFIG, Style

class QuizEngine:
    def __init__(self):
        self.config = CONFIG
        self.db = {}
        self.db_url = os.getenv("DATABASE_URL")
        if self.db_url:
            print(f"{Style.GREEN}[DATABASE] PostgreSQL detected in environment.{Style.RESET}")
        else:
            print(f"{Style.YELLOW}[DATABASE] Running without cloud database environment.{Style.RESET}")

    def get_db_connection(self):
        """Creates a fresh database connection to PostgreSQL."""
        if not self.db_url:
            raise ConnectionError("DATABASE_URL environment variable is missing.")
        return psycopg2.connect(self.db_url, cursor_factory=RealDictCursor)

    # --- POSTGRES STATE METHODS ---
    def db_save_track(self, message_id, q_id, status, display_id, type_, msg_type, followup_mid=None):
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
            print(f"{Style.RED}[DB ERROR] Failed to save track: {e}{Style.RESET}")

    def db_get_all_tracks(self):
        try:
            conn = self.get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT * FROM sent_tracks;")
            rows = cur.fetchall()
            cur.close()
            conn.close()
            return {r['message_id']: dict(r) for r in rows}
        except Exception as e:
            print(f"{Style.RED}[DB ERROR] Failed to retrieve tracks: {e}{Style.RESET}")
            return {}

    def db_update_track_status(self, message_id, status, followup_mid=None):
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
            print(f"{Style.RED}[DB ERROR] Failed to update track status: {e}{Style.RESET}")

    # --- FILE FALLBACK SYSTEM FOR LOCAL ADMIN BACKUPS ---
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

    def refresh_database(self):
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