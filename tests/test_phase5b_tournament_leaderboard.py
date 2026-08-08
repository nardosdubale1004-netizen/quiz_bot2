# tests/test_phase5b_tournament_leaderboard.py
import pytest
from tests.conftest import TEST_USER_ID

TEST_Q = "TESTQ-TLB-001"
TEST_MID_TOURNEY = "8880004001"
TEST_MID_REGULAR = "8880004002"
RUN_ID_A = "test-run-aaa"


@pytest.fixture
def clean_tlb(engine):
    def _wipe():
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                for mid in (TEST_MID_TOURNEY, TEST_MID_REGULAR):
                    cur.execute("DELETE FROM user_responses WHERE message_id = %s;", (mid,))
                    cur.execute("DELETE FROM sent_tracks WHERE message_id = %s;", (mid,))
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


def test_tournament_leaderboard_excludes_non_tournament_answers(engine, clean_test_users, clean_tlb):
    """A user answers ONE tournament-tagged question and ONE regular (non-tournament) question.
    The tournament-scoped leaderboard must show ONLY the tournament answer's marks,
    while the user's lifetime total_marks reflects BOTH."""
    from src.database import (
        db_update_user_telegram_info, db_try_start_tournament_round,
        process_user_score, db_get_tournament_leaderboard, db_get_user_profile
    )

    db_update_user_telegram_info(TEST_USER_ID, "tlb_test", "TLB Test")

    # Regular (non-tournament) sent_track — plain active question, backdated to 'standard' speed tier
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO sent_tracks (message_id, q_id, status, display_id, type, msg_type, sent_at)
                VALUES (%s, %s, 'active', 601, 'premium', 'text', NOW() - INTERVAL '10 minutes');
            """, (TEST_MID_REGULAR, TEST_Q))
        conn.commit()
    finally:
        engine.release_connection(conn)

    # Tournament-tagged sent_track, claimed via the advisory-lock function with a run_id.
    # db_try_start_tournament_round leaves sent_at at its schema default (NOW()) — backdate it
    # AFTER claiming so speed-tier scoring isn't accidentally exercised (round_deadline, which is
    # based on round_seconds from claim time, is untouched and stays valid for 300s).
    claimed = db_try_start_tournament_round(
        TEST_MID_TOURNEY, TEST_Q, 602, round_seconds=300,
        round_number=1, total_rounds=1, tournament_run_id=RUN_ID_A
    )
    assert claimed is True

    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sent_tracks SET sent_at = NOW() - INTERVAL '10 minutes' WHERE message_id = %s;",
                (TEST_MID_TOURNEY,)
            )
        conn.commit()
    finally:
        engine.release_connection(conn)

    # Answer the regular question first
    r_regular = process_user_score(TEST_USER_ID, TEST_MID_REGULAR, TEST_Q, True, 0)
    assert r_regular["marks_awarded"] == 3

    # Answer the tournament question
    r_tourney = process_user_score(TEST_USER_ID, TEST_MID_TOURNEY, TEST_Q, True, 0, None, False, False)
    assert r_tourney["speed_tier"] == "standard"  # sanity check the backdate worked
    assert r_tourney["marks_awarded"] == 3

    # Lifetime total must include BOTH
    profile = db_get_user_profile(TEST_USER_ID)
    assert profile["total_marks"] == 6

    # Tournament-scoped leaderboard must ONLY reflect the tournament answer
    rows = db_get_tournament_leaderboard(RUN_ID_A)
    assert len(rows) == 1
    assert rows[0]["tournament_score"] == 3  # NOT 6 — must not include the regular answer
    assert rows[0]["tournament_correct"] == 1


def test_tournament_leaderboard_empty_run_id_returns_empty(engine, clean_test_users):
    """A run_id with no rounds at all should return an empty list, not error."""
    from src.database import db_get_tournament_leaderboard

    rows = db_get_tournament_leaderboard("nonexistent-run-id-xyz")
    assert rows == []