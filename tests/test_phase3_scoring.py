# tests/test_phase3_scoring.py
import pytest
from tests.conftest import TEST_USER_ID, TEST_USER_ID_2


TEST_Q_EASY = "TESTQ-EASY-001"
TEST_Q_MEDIUM = "TESTQ-MED-001"
TEST_Q_HARD = "TESTQ-HARD-001"
TEST_MID_EASY = "8880000001"
TEST_MID_MEDIUM = "8880000002"
TEST_MID_HARD = "8880000003"


@pytest.fixture
def clean_test_questions(engine):
    """Creates 3 throwaway questions (easy/medium/hard) + matching sent_tracks rows,
    wiped before and after each test.

    IMPORTANT: sent_at is set 10 minutes in the past (not NOW()) so tests land in the
    'standard' speed tier (x1.0) instead of accidentally triggering the 'lightning'
    (<=60s, x1.5) or 'fast' (<=300s, x1.2) speed bonuses. This isolates pure
    base-difficulty scoring — speed-tier behavior gets its own dedicated test (3.2).
    """
    def _wipe():
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                for mid in (TEST_MID_EASY, TEST_MID_MEDIUM, TEST_MID_HARD):
                    cur.execute("DELETE FROM user_responses WHERE message_id = %s;", (mid,))
                    cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (mid,))
                for qid in (TEST_Q_EASY, TEST_Q_MEDIUM, TEST_Q_HARD):
                    cur.execute("DELETE FROM questions WHERE id = %s;", (qid,))
            conn.commit()
        finally:
            engine.release_connection(conn)

    _wipe()

    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            for qid, mid, diff in [
                (TEST_Q_EASY, TEST_MID_EASY, "easy"),
                (TEST_Q_MEDIUM, TEST_MID_MEDIUM, "medium"),
                (TEST_Q_HARD, TEST_MID_HARD, "hard"),
            ]:
                cur.execute("""
                    INSERT INTO questions (id, subject, topic, difficulty, tags, question, options, correct_option)
                    VALUES (%s, 'mathematics', 'Test Topic', %s, ARRAY[]::text[], 'Test question?', ARRAY['A','B','C','D'], 0);
                """, (qid, diff))
                cur.execute("""
                    INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, sent_at)
                    VALUES (%s, %s, 'active', %s, 'premium', 'text', NOW() - INTERVAL '10 minutes');
                """, (mid, qid, int(mid[-3:])))
        conn.commit()
    finally:
        engine.release_connection(conn)

    yield

    _wipe()


# ---------- 3.1: base difficulty points (isolated from speed/grade/streak multipliers) ----------

def test_easy_medium_hard_base_points(engine, clean_test_users, clean_test_questions):
    from src.database import db_update_user_telegram_info, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "score_test", "Score Test")

    result_easy = process_user_score(TEST_USER_ID, TEST_MID_EASY, TEST_Q_EASY, True, 0)
    assert result_easy is not None
    assert result_easy["speed_tier"] == "standard"  # sanity check that the fixture worked
    assert result_easy["marks_awarded"] == 3

    result_medium = process_user_score(TEST_USER_ID, TEST_MID_MEDIUM, TEST_Q_MEDIUM, True, 0)
    assert result_medium["marks_awarded"] == 6

    result_hard = process_user_score(TEST_USER_ID, TEST_MID_HARD, TEST_Q_HARD, True, 0)
    assert result_hard["marks_awarded"] == 12


# ---------- 3.5: answering same question twice returns first result, no double marks ----------

def test_double_answer_returns_first_result_only(engine, clean_test_users, clean_test_questions):
    from src.database import db_update_user_telegram_info, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "double_test", "Double Test")

    first = process_user_score(TEST_USER_ID, TEST_MID_EASY, TEST_Q_EASY, True, 0)
    assert first["marks_awarded"] == 3
    assert first["first_try"] is True

    second = process_user_score(TEST_USER_ID, TEST_MID_EASY, TEST_Q_EASY, False, 2)
    assert second["first_try"] is False
    assert second["marks_awarded"] == 3  # locked to original result
    assert second["total_marks"] == first["total_marks"]  # no double-counting


# ---------- 3.6: incorrect answer awards 0 marks but total/correct still update ----------

def test_incorrect_answer_awards_zero_marks(engine, clean_test_users, clean_test_questions):
    from src.database import db_update_user_telegram_info, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "wrong_test", "Wrong Test")

    result = process_user_score(TEST_USER_ID, TEST_MID_EASY, TEST_Q_EASY, False, 3)
    assert result is not None
    assert result["marks_awarded"] == 0
    assert result["total"] == 1
    assert result["correct"] == 0


# ---------- 3.8: subject marks only increment on correct answers with marks > 0 ----------

def test_subject_marks_only_increment_on_correct(engine, clean_test_users, clean_test_questions):
    from src.database import db_update_user_telegram_info, process_user_score, db_get_user_subject_marks

    db_update_user_telegram_info(TEST_USER_ID, "subj_test", "Subj Test")

    process_user_score(TEST_USER_ID, TEST_MID_EASY, TEST_Q_EASY, False, 2)
    marks_after_wrong = db_get_user_subject_marks(TEST_USER_ID)
    assert marks_after_wrong == []

    process_user_score(TEST_USER_ID, TEST_MID_MEDIUM, TEST_Q_MEDIUM, True, 0)
    marks_after_correct = db_get_user_subject_marks(TEST_USER_ID)
    assert len(marks_after_correct) == 1
    assert marks_after_correct[0]["subject"] == "mathematics"
    assert marks_after_correct[0]["marks"] == 6


# ---------- 3.7: referral bonus, 2-level cap ----------

# tests/test_phase3_scoring.py  — replace ONLY test_referral_bonus_two_level_cap with this

def test_referral_bonus_two_level_cap(engine, clean_test_users, clean_test_questions):
    from src.database import db_update_user_telegram_info, db_set_user_referrer, process_user_score

    referrer_a = "9999990101"
    referrer_none_yet = TEST_USER_ID  # acts as B

    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_stats WHERE user_id = %s;", (referrer_a,))
        conn.commit()
    finally:
        engine.release_connection(conn)

    try:
        db_update_user_telegram_info(referrer_a, "referrer_a", "A")
        db_update_user_telegram_info(referrer_none_yet, "referred_b", "B")

        linked = db_set_user_referrer(referrer_none_yet, referrer_a)
        assert linked is True

        result = process_user_score(referrer_none_yet, TEST_MID_EASY, TEST_Q_EASY, True, 0)
        assert result["marks_awarded"] == 3

        # Bypass the profile cache entirely — query DB directly to isolate SQL-function
        # correctness from Python-layer caching behavior.
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT total_marks FROM user_stats WHERE user_id = %s;", (referrer_a,))
                row = cur.fetchone()
        finally:
            engine.release_connection(conn)

        assert row["total_marks"] == 1  # 5% of 3 = 0.15 -> GREATEST(1, floor(...)) = 1
    finally:
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_stats WHERE user_id = %s;", (referrer_a,))
            conn.commit()
        finally:
            engine.release_connection(conn)