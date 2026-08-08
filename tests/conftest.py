# tests/conftest.py
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_USER_ID = "9999990001"
TEST_USER_ID_2 = "9999990002"


@pytest.fixture(scope="session")
def engine():
    from src.database import QuizEngine
    eng = QuizEngine()
    if not eng.db_url:
        pytest.skip("DATABASE_URL not set — skipping DB-backed tests")
    return eng


@pytest.fixture(scope="session")
def db_conn(engine):
    conn = engine.get_db_connection()
    yield conn
    engine.release_connection(conn)


@pytest.fixture
def clean_test_users(engine):
    """Wipes leftover test rows before AND after each test using this fixture,
    so no test ever sees stale state from a previous failed run."""
    def _wipe():
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                for uid in (TEST_USER_ID, TEST_USER_ID_2):
                    cur.execute("DELETE FROM user_responses WHERE user_id = %s;", (uid,))
                    cur.execute("DELETE FROM user_subject_marks WHERE user_id = %s;", (uid,))
                    cur.execute("DELETE FROM user_difficulty_marks WHERE user_id = %s;", (uid,))
                    cur.execute("DELETE FROM user_org_contributions WHERE user_id = %s;", (uid,))
                    cur.execute("DELETE FROM user_geo_contributions WHERE user_id = %s;", (uid,))
                    cur.execute("DELETE FROM org_memberships WHERE user_id = %s;", (uid,))
                    cur.execute("DELETE FROM user_locations WHERE user_id = %s;", (uid,))
                    cur.execute("DELETE FROM user_stats WHERE user_id = %s;", (uid,))
            conn.commit()
        finally:
            engine.release_connection(conn)
    _wipe()
    yield
    _wipe()