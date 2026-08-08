# tests/test_phase3b_multipliers.py
import pytest
from tests.conftest import TEST_USER_ID


TEST_Q_A = "TESTQ-MULT-A"
TEST_Q_B = "TESTQ-MULT-B"
TEST_Q_C = "TESTQ-MULT-C"
TEST_MID_A = "8880001001"
TEST_MID_B = "8880001002"
TEST_MID_C = "8880001003"


def _make_question(engine, qid, mid, difficulty, tags, sent_at_offset_seconds):
    """sent_at_offset_seconds: how many seconds AGO the question was sent.
    Use small values to land inside speed-bonus windows, large values for 'standard'."""
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO questions (id, subject, topic, difficulty, tags, question, options, correct_option)
                VALUES (%s, 'mathematics', 'Test Topic', %s, %s, 'Test question?', ARRAY['A','B','C','D'], 0);
            """, (qid, difficulty, tags))
            cur.execute("""
                INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, sent_at)
                VALUES (%s, %s, 'active', %s, 'premium', 'text', NOW() - (%s || ' seconds')::interval);
            """, (mid, qid, int(mid[-3:]), sent_at_offset_seconds))
        conn.commit()
    finally:
        engine.release_connection(conn)


def _wipe(engine):
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            for mid in (TEST_MID_A, TEST_MID_B, TEST_MID_C):
                cur.execute("DELETE FROM user_responses WHERE message_id = %s;", (mid,))
                cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (mid,))
            for qid in (TEST_Q_A, TEST_Q_B, TEST_Q_C):
                cur.execute("DELETE FROM questions WHERE id = %s;", (qid,))
        conn.commit()
    finally:
        engine.release_connection(conn)


@pytest.fixture
def clean_mult_questions(engine):
    _wipe(engine)
    yield
    _wipe(engine)


# ---------- 3.2: speed multiplier boundaries ----------

def test_speed_multiplier_lightning_fast_standard(engine, clean_test_users, clean_mult_questions):
    from src.database import db_update_user_telegram_info, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "speed_test", "Speed Test")

    _make_question(engine, TEST_Q_A, TEST_MID_A, "easy", [], 30)    # 30s ago -> lightning x1.5
    _make_question(engine, TEST_Q_B, TEST_MID_B, "easy", [], 120)   # 2min ago -> fast x1.2
    _make_question(engine, TEST_Q_C, TEST_MID_C, "easy", [], 600)   # 10min ago -> standard x1.0

    r_lightning = process_user_score(TEST_USER_ID, TEST_MID_A, TEST_Q_A, True, 0)
    assert r_lightning["speed_tier"] == "lightning"
    assert r_lightning["marks_awarded"] == 4  # floor(3 * 1.5) = 4

    r_fast = process_user_score(TEST_USER_ID, TEST_MID_B, TEST_Q_B, True, 0)
    assert r_fast["speed_tier"] == "fast"
    assert r_fast["marks_awarded"] == 3  # floor(3 * 1.2) = 3

    r_standard = process_user_score(TEST_USER_ID, TEST_MID_C, TEST_Q_C, True, 0)
    assert r_standard["speed_tier"] == "standard"
    assert r_standard["marks_awarded"] == 3  # floor(3 * 1.0) = 3


# ---------- 3.3: grade challenge multiplier ----------

def test_grade_challenge_multiplier(engine, clean_test_users, clean_mult_questions):
    from src.database import db_update_user_telegram_info, db_set_user_grade, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "grade_mult_test", "Grade Mult Test")
    db_set_user_grade(TEST_USER_ID, 8)  # user is grade 8

    # Above-grade question (grade12 tag) -> x1.5 challenge bonus
    _make_question(engine, TEST_Q_A, TEST_MID_A, "easy", ["grade12"], 600)
    # Same-grade question (grade8 tag) -> x1.0
    _make_question(engine, TEST_Q_B, TEST_MID_B, "easy", ["grade8"], 600)
    # Below-grade question (grade6 tag) -> x0.3
    _make_question(engine, TEST_Q_C, TEST_MID_C, "easy", ["grade6"], 600)

    r_above = process_user_score(TEST_USER_ID, TEST_MID_A, TEST_Q_A, True, 0)
    assert r_above["marks_awarded"] == 4  # floor(3 * 1.5) = 4

    r_same = process_user_score(TEST_USER_ID, TEST_MID_B, TEST_Q_B, True, 0)
    assert r_same["marks_awarded"] == 3  # floor(3 * 1.0) = 3

    r_below = process_user_score(TEST_USER_ID, TEST_MID_C, TEST_Q_C, True, 0)
    assert r_below["marks_awarded"] == 1  # GREATEST(1, floor(3 * 0.3)) = GREATEST(1, 0) = 1