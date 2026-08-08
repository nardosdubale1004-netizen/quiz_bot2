# tests/test_phase5a_tournament_db.py
import pytest
from tests.conftest import TEST_USER_ID, TEST_USER_ID_2

TEST_Q = "TESTQ-TOURNEY-001"
TEST_MID = "8880003001"


@pytest.fixture
def clean_tourney_round(engine):
    def _wipe():
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM user_responses WHERE message_id = %s;", (TEST_MID,))
                cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (TEST_MID,))
                cur.execute("DELETE FROM questions WHERE id = %s;", (TEST_Q,))
            conn.commit()
        finally:
            engine.release_connection(conn)
    _wipe()

    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO questions (id, subject, topic, difficulty, tags, question, options, correct_option)
                VALUES (%s, 'mathematics', 'Test Topic', 'easy', ARRAY[]::text[], 'Test question?', ARRAY['A','B','C','D'], 0);
            """, (TEST_Q,))
        conn.commit()
    finally:
        engine.release_connection(conn)

    yield
    _wipe()


# ---------- 5.2: only ONE round can be tournament_active at a time (advisory lock) ----------

def test_only_one_active_round_at_a_time(engine, clean_test_users, clean_tourney_round):
    from src.database import db_try_start_tournament_round, db_get_active_tournament_rounds

    claimed_1 = db_try_start_tournament_round(TEST_MID, TEST_Q, 501, round_seconds=60, round_number=1, total_rounds=1)
    assert claimed_1 is True

    # Attempt a second concurrent round while one is already active
    other_mid = "8880003002"
    claimed_2 = db_try_start_tournament_round(other_mid, TEST_Q, 502, round_seconds=60, round_number=1, total_rounds=1)
    assert claimed_2 is False  # blocked — a round is already tournament_active

    active = db_get_active_tournament_rounds()
    active_mids = [r["message_id"] for r in active]
    assert TEST_MID in active_mids
    assert other_mid not in active_mids

    # cleanup the extra track this test created
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (other_mid,))
        conn.commit()
    finally:
        engine.release_connection(conn)


# ---------- 5.4 building block: db_edit_tournament_answer flip tracking ----------

def test_edit_tournament_answer_flip_tracking(engine, clean_test_users, clean_tourney_round):
    from src.database import (
        db_update_user_telegram_info, db_try_start_tournament_round,
        process_user_score, db_edit_tournament_answer, db_get_user_edit_stats
    )

    db_update_user_telegram_info(TEST_USER_ID, "flip_test", "Flip Test")
    claimed = db_try_start_tournament_round(TEST_MID, TEST_Q, 503, round_seconds=300, round_number=1, total_rounds=1)
    assert claimed is True

    # First answer: wrong (option 2, correct is 0)
    first = process_user_score(TEST_USER_ID, TEST_MID, TEST_Q, False, 2, None, False, False)
    assert first["marks_awarded"] == 0

    # Edit to correct option -> should flip "hurt/neutral" -> "helped"
    flip_result = db_edit_tournament_answer(TEST_USER_ID, TEST_MID, new_option=0, new_is_correct=True)
    assert flip_result is not None
    assert flip_result["o_result_flip"] == "helped"
    assert flip_result["o_marks_awarded"] > 0

    stats = db_get_user_edit_stats(TEST_USER_ID)
    assert stats["answer_edits_total"] == 1
    assert stats["answer_edits_helped"] == 1
    assert stats["answer_edits_hurt"] == 0


# ---------- 5.4b: editing FROM correct TO incorrect flips "hurt" ----------

def test_edit_tournament_answer_hurt_flip(engine, clean_test_users, clean_tourney_round):
    from src.database import (
        db_update_user_telegram_info, db_try_start_tournament_round,
        process_user_score, db_edit_tournament_answer, db_get_user_edit_stats
    )

    db_update_user_telegram_info(TEST_USER_ID, "hurt_flip_test", "Hurt Flip")
    db_try_start_tournament_round(TEST_MID, TEST_Q, 504, round_seconds=300, round_number=1, total_rounds=1)

    first = process_user_score(TEST_USER_ID, TEST_MID, TEST_Q, True, 0, None, False, False)
    assert first["marks_awarded"] > 0

    flip_result = db_edit_tournament_answer(TEST_USER_ID, TEST_MID, new_option=2, new_is_correct=False)
    assert flip_result is not None
    assert flip_result["o_result_flip"] == "hurt"
    assert flip_result["o_marks_awarded"] == 0

    stats = db_get_user_edit_stats(TEST_USER_ID)
    assert stats["answer_edits_hurt"] == 1


# ---------- 5.6 building block: overdue detection ----------

def test_overdue_round_detection(engine, clean_test_users, clean_tourney_round):
    from src.database import db_try_start_tournament_round, db_get_overdue_tournament_rounds

    # round_seconds=0 with a slight negative offset makes it instantly overdue
    claimed = db_try_start_tournament_round(TEST_MID, TEST_Q, 505, round_seconds=0, round_number=1, total_rounds=1)
    assert claimed is True

    import time
    time.sleep(1.2)  # ensure the deadline (NOW()+0s at claim time) is now in the past

    overdue = db_get_overdue_tournament_rounds()
    overdue_mids = [r["message_id"] for r in overdue]
    assert TEST_MID in overdue_mids