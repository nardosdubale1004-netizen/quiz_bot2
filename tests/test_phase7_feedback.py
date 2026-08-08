# tests/test_phase7_feedback.py
import pytest
from tests.conftest import TEST_USER_ID, TEST_USER_ID_2


@pytest.fixture
def clean_feedback(engine):
    def _wipe():
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM feedback_messages WHERE feedback_id IN (SELECT id FROM feedback WHERE user_id IN (%s, %s));", (TEST_USER_ID, TEST_USER_ID_2))
                cur.execute("DELETE FROM feedback WHERE user_id IN (%s, %s);", (TEST_USER_ID, TEST_USER_ID_2))
                cur.execute("DELETE FROM location_suggestion_messages WHERE suggestion_id IN (SELECT id FROM location_suggestions WHERE submitted_by IN (%s, %s));", (TEST_USER_ID, TEST_USER_ID_2))
                cur.execute("DELETE FROM location_suggestions WHERE submitted_by IN (%s, %s);", (TEST_USER_ID, TEST_USER_ID_2))
            conn.commit()
        finally:
            engine.release_connection(conn)
    _wipe()
    yield
    _wipe()


# ---------- 7.1: submit feedback -> appears, correct status ----------

def test_submit_feedback_creates_open_item(engine, clean_test_users, clean_feedback):
    from src.database import db_update_user_telegram_info, db_submit_feedback, db_get_feedback_by_id

    db_update_user_telegram_info(TEST_USER_ID, "fb_submit_test", "FB Test")
    fid = db_submit_feedback(TEST_USER_ID, "bug", "The leaderboard shows the wrong rank")
    assert fid is not None

    fb = db_get_feedback_by_id(fid)
    assert fb["status"] == "open"
    assert fb["category"] == "bug"
    assert fb["is_closed"] in (False, None)


# ---------- 7.2: status transitions ----------

def test_feedback_status_transitions(engine, clean_test_users, clean_feedback):
    from src.database import (
        db_update_user_telegram_info, db_submit_feedback, db_update_feedback_status, db_get_feedback_by_id
    )

    db_update_user_telegram_info(TEST_USER_ID, "fb_status_test", "FB Status")
    fid = db_submit_feedback(TEST_USER_ID, "feature", "Add dark mode")

    for status in ["in_progress", "planned", "resolved"]:
        ok = db_update_feedback_status(fid, status)
        assert ok is True
        fb = db_get_feedback_by_id(fid)
        assert fb["status"] == status


# ---------- 7.3: closing blocks replies, reopening restores ----------

def test_feedback_close_and_reopen(engine, clean_test_users, clean_feedback):
    from src.database import (
        db_update_user_telegram_info, db_submit_feedback, db_set_feedback_closed, db_get_feedback_by_id
    )

    db_update_user_telegram_info(TEST_USER_ID, "fb_close_test", "FB Close")
    fid = db_submit_feedback(TEST_USER_ID, "general", "Just some thoughts")

    ok = db_set_feedback_closed(fid, True)
    assert ok is True
    fb = db_get_feedback_by_id(fid)
    assert fb["is_closed"] is True

    ok2 = db_set_feedback_closed(fid, False)
    assert ok2 is True
    fb2 = db_get_feedback_by_id(fid)
    assert fb2["is_closed"] is False


# ---------- 7.4: /myfeedback lists BOTH feedback and location suggestions, sorted, is_closed defaults False ----------

def test_user_feedback_and_requests_list_combines_both_kinds(engine, clean_test_users, clean_feedback):
    from src.database import (
        db_update_user_telegram_info, db_submit_feedback, db_create_location_suggestion,
        db_get_user_feedback_and_requests, db_count_user_feedback_and_requests
    )

    db_update_user_telegram_info(TEST_USER_ID, "fb_combo_test", "FB Combo")
    fid = db_submit_feedback(TEST_USER_ID, "bug", "Combo test feedback")
    sid = db_create_location_suggestion("city", "ComboTestCity", "Ethiopia", TEST_USER_ID)
    assert sid is not None

    total = db_count_user_feedback_and_requests(TEST_USER_ID)
    assert total == 2

    items = db_get_user_feedback_and_requests(TEST_USER_ID, limit=10, offset=0)
    assert len(items) == 2
    kinds = {item["kind"] for item in items}
    assert kinds == {"feedback", "city"}
    for item in items:
        assert item["is_closed"] in (False, None)  # never crashes on missing column, defaults correctly


# ---------- 7.5-adjacent: feedback messages thread works ----------

def test_feedback_message_thread_and_admin_reply_updates_status(engine, clean_test_users, clean_feedback):
    from src.database import (
        db_update_user_telegram_info, db_submit_feedback, db_add_feedback_message,
        db_get_feedback_thread, db_get_feedback_by_id
    )

    db_update_user_telegram_info(TEST_USER_ID, "fb_thread_test", "FB Thread")
    fid = db_submit_feedback(TEST_USER_ID, "bug", "Original message")

    ok = db_add_feedback_message(fid, "admin", "1", "We're looking into it")
    assert ok is True

    fb = db_get_feedback_by_id(fid)
    assert fb["status"] == "in_progress"  # admin reply auto-flips status
    assert fb["admin_reply"] == "We're looking into it"

    thread = db_get_feedback_thread(fid)
    assert len(thread) == 1
    assert thread[0]["sender_role"] == "admin"


# ---------- location suggestion resolve cascade (linked city+school) ----------

def test_linked_location_suggestions_cascade_on_resolve(engine, clean_test_users, clean_feedback):
    from src.database import (
        db_update_user_telegram_info, db_create_location_suggestion, db_link_location_suggestions,
        db_resolve_location_suggestion, db_get_location_suggestion
    )

    db_update_user_telegram_info(TEST_USER_ID, "fb_linked_test", "FB Linked")

    sid_city = db_create_location_suggestion("city", "LinkedCityXYZ", "Ethiopia", TEST_USER_ID)
    sid_school = db_create_location_suggestion("school", "Linked School XYZ", "Ethiopia", TEST_USER_ID)
    assert sid_city is not None and sid_school is not None

    linked = db_link_location_suggestions(sid_city, sid_school)
    assert linked is True

    result = db_resolve_location_suggestion(sid_city, admin_id="1", approve=True)
    assert result is not None
    assert "linked_suggestion" in result
    assert result["linked_suggestion"]["id"] == sid_school

    school_after = db_get_location_suggestion(sid_school)
    assert school_after["status"] == "approved"  # cascaded from the city resolution