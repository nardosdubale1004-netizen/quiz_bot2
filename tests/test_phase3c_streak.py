# tests/test_phase3c_streak.py
import pytest
from tests.conftest import TEST_USER_ID

TEST_Q = "TESTQ-STREAK-001"
TEST_MID = "8880002001"


def _make_question(engine, days_since_last_active=None, seed_streak=0):
    """Creates one throwaway question + sent_track (backdated, so it's always 'standard' speed).
    If days_since_last_active is not None, seeds user_stats.last_active_at that many days in
    the past and current_streak = seed_streak, simulating a user returning after a gap."""
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_responses WHERE message_id = %s;", (TEST_MID,))
            cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (TEST_MID,))
            cur.execute("DELETE FROM questions WHERE id = %s;", (TEST_Q,))
            cur.execute("""
                INSERT INTO questions (id, subject, topic, difficulty, tags, question, options, correct_option)
                VALUES (%s, 'mathematics', 'Test Topic', 'easy', ARRAY[]::text[], 'Test question?', ARRAY['A','B','C','D'], 0);
            """, (TEST_Q,))
            cur.execute("""
                INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, sent_at)
                VALUES (%s, %s, 'active', %s, 'premium', 'text', NOW() - INTERVAL '10 minutes');
            """, (TEST_MID, TEST_Q, int(TEST_MID[-3:])))

            if days_since_last_active is not None:
                cur.execute("""
                    UPDATE user_stats
                    SET last_active_at = NOW() - (%s || ' days')::interval, current_streak = %s
                    WHERE user_id = %s;
                """, (days_since_last_active, seed_streak, TEST_USER_ID))
        conn.commit()
    finally:
        engine.release_connection(conn)


@pytest.fixture
def clean_streak_question(engine):
    yield
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_responses WHERE message_id = %s;", (TEST_MID,))
            cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (TEST_MID,))
            cur.execute("DELETE FROM questions WHERE id = %s;", (TEST_Q,))
        conn.commit()
    finally:
        engine.release_connection(conn)


# ---------- 3.4: streak multiplier tiers ----------

def test_streak_day_one_no_bonus(engine, clean_test_users, clean_streak_question):
    """Brand new user, first-ever answer -> last_active_at is NULL -> streak becomes 1 -> x1.0."""
    from src.database import db_update_user_telegram_info, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "streak_d1", "Streak D1")
    _make_question(engine, days_since_last_active=None)  # no seeding — fresh user

    result = process_user_score(TEST_USER_ID, TEST_MID, TEST_Q, True, 0)
    assert result["current_streak"] == 1
    assert result["marks_awarded"] == 3  # base easy(3) * speed(1.0) * grade(1.0) * streak(1.0)


def test_streak_day_three_gives_1_2x(engine, clean_test_users, clean_streak_question):
    """User last active exactly 1 day ago with streak already at 2 -> today extends to streak=3 -> x1.2."""
    from src.database import db_update_user_telegram_info, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "streak_d3", "Streak D3")
    _make_question(engine, days_since_last_active=1, seed_streak=2)

    result = process_user_score(TEST_USER_ID, TEST_MID, TEST_Q, True, 0)
    assert result["current_streak"] == 3
    assert result["marks_awarded"] == 3  # floor(3 * 1.2) = 3 (rounds down, same as base here)


def test_streak_day_seven_gives_1_5x(engine, clean_test_users, clean_streak_question):
    """User last active exactly 1 day ago with streak already at 6 -> today extends to streak=7 -> x1.5."""
    from src.database import db_update_user_telegram_info, process_user_score

    db_update_user_telegram_info(TEST_USER_ID, "streak_d7", "Streak D7")
    _make_question(engine, days_since_last_active=1, seed_streak=6)

    result = process_user_score(TEST_USER_ID, TEST_MID, TEST_Q, True, 0)
    assert result["current_streak"] == 7
    assert result["marks_awarded"] == 4  # floor(3 * 1.5) = 4


def test_streak_resets_after_missed_day(engine, clean_test_users, clean_streak_question):
    """User last active 3 days ago with a streak of 7 -> gap > 1 day -> streak resets to 1 -> x1.0.
    Also confirms lifetime total_marks is NEVER reduced by a streak reset."""
    from src.database import db_update_user_telegram_info, process_user_score, db_get_user_profile

    db_update_user_telegram_info(TEST_USER_ID, "streak_reset", "Streak Reset")
    _make_question(engine, days_since_last_active=3, seed_streak=7)

    # Seed a prior lifetime score directly, to prove it survives the reset untouched
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_stats SET total_marks = 500 WHERE user_id = %s;", (TEST_USER_ID,))
        conn.commit()
    finally:
        engine.release_connection(conn)

    result = process_user_score(TEST_USER_ID, TEST_MID, TEST_Q, True, 0)
    assert result["current_streak"] == 1  # reset, not carried over
    assert result["marks_awarded"] == 3   # x1.0, no streak bonus
    assert result["total_marks"] == 503   # 500 (preserved) + 3 (this answer) — never wiped