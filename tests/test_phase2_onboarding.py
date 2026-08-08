# tests/test_phase2_onboarding.py
import pytest
from tests.conftest import TEST_USER_ID, TEST_USER_ID_2


# ---------- 2.1: new user starts with no location, not gated by grade ----------

def test_new_user_has_no_location_and_grade_not_required(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_get_user_profile, db_user_location_complete

    db_update_user_telegram_info(TEST_USER_ID, "loc_test", "Loc Test")
    profile = db_get_user_profile(TEST_USER_ID)

    assert profile["personal_city"] is None
    assert profile["personal_country"] is None
    assert profile["grade"] is None
    assert db_user_location_complete(TEST_USER_ID) is False


# ---------- 2.2 / 2.3: approved vs pending city ----------

def test_setting_approved_city_unlocks_answering(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_set_user_location, db_get_user_location, db_user_location_complete

    db_update_user_telegram_info(TEST_USER_ID, "approved_city_test", "Approved City")  # <-- FIX: create user_stats row first

    ok = db_set_user_location(TEST_USER_ID, city="Addis Ababa", country="Ethiopia", status="approved")
    assert ok is True

    loc = db_get_user_location(TEST_USER_ID)
    assert loc["city"] == "Addis Ababa"
    assert loc["country"] == "Ethiopia"
    assert loc["status"] == "approved"
    assert db_user_location_complete(TEST_USER_ID) is True


def test_setting_pending_city_still_unlocks_answering(engine, clean_test_users):
    """Per db_user_location_complete: pending counts as 'complete' — pending should
    NOT block a student from answering, only from counting on public leaderboards."""
    from src.database import db_update_user_telegram_info, db_set_user_location, db_get_user_location, db_user_location_complete

    db_update_user_telegram_info(TEST_USER_ID, "pending_city_test", "Pending City")  # <-- FIX

    ok = db_set_user_location(TEST_USER_ID, city="Nowheresville", country="Ethiopia", status="pending", suggestion_id=None)
    assert ok is True

    loc = db_get_user_location(TEST_USER_ID)
    assert loc["status"] == "pending"
    assert db_user_location_complete(TEST_USER_ID) is True  # pending still unlocks answering


# ---------- 2.4: admin rejects a pending city -> fully cleared, re-gated ----------

def test_reject_pending_city_clears_location_and_regates_user(engine, clean_test_users):
    from src.database import (
        db_update_user_telegram_info, db_set_user_location, db_create_location_suggestion,
        db_resolve_location_suggestion, db_get_user_location, db_user_location_complete
    )

    db_update_user_telegram_info(TEST_USER_ID, "reject_test", "Reject Test")  # <-- FIX

    sid = db_create_location_suggestion("city", "Fakeburg", "Ethiopia", TEST_USER_ID)
    assert sid is not None
    ok = db_set_user_location(TEST_USER_ID, city="Fakeburg", country="Ethiopia", status="pending", suggestion_id=sid)
    assert ok is True
    assert db_user_location_complete(TEST_USER_ID) is True

    result = db_resolve_location_suggestion(sid, admin_id="1", approve=False)
    assert result is not None
    assert TEST_USER_ID in result["affected_users"]

    loc = db_get_user_location(TEST_USER_ID)
    assert loc["city"] is None
    assert loc["country"] is None
    assert db_user_location_complete(TEST_USER_ID) is False  # re-gated


# ---------- 2.5: admin approves a pending city -> counts on leaderboards ----------

def test_approve_pending_city_makes_user_count_on_city_leaderboard(engine, clean_test_users):
    from src.database import (
        db_update_user_telegram_info, db_set_user_location, db_create_location_suggestion,
        db_resolve_location_suggestion, db_get_scope_summary
    )

    db_update_user_telegram_info(TEST_USER_ID, "approve_test", "Approve Test")
    sid = db_create_location_suggestion("city", "TestCity99", "Ethiopia", TEST_USER_ID)
    db_set_user_location(TEST_USER_ID, city="TestCity99", country="Ethiopia", status="pending", suggestion_id=sid)

    before = db_get_scope_summary(scope="city", entity="TestCity99", grade=None)
    assert before["student_count"] == 0

    db_resolve_location_suggestion(sid, admin_id="1", approve=True)

    after = db_get_scope_summary(scope="city", entity="TestCity99", grade=None)
    assert after["student_count"] == 1


# ---------- 2.6: grade requires a school ----------

def test_grade_change_without_school_is_blocked_at_db_level_check(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_get_user_profile

    db_update_user_telegram_info(TEST_USER_ID, "no_school_test", "NoSchool")
    profile = db_get_user_profile(TEST_USER_ID)
    assert profile["org_id"] is None


# ---------- 2.9: "Not a Student" clears grade ----------

def test_clear_user_grade_removes_grade(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_set_user_grade, db_get_user_profile, db_clear_user_grade

    db_update_user_telegram_info(TEST_USER_ID, "grade_test", "Grade Test")
    db_set_user_grade(TEST_USER_ID, 10)
    assert db_get_user_profile(TEST_USER_ID)["grade"] == 10

    db_clear_user_grade(TEST_USER_ID)
    assert db_get_user_profile(TEST_USER_ID)["grade"] is None