# tests/test_phase9_permissions.py
import pytest
from tests.conftest import TEST_USER_ID


# ---------- 9.1: fails OPEN when no row exists ----------

def test_permission_check_fails_open_when_no_row(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_check_user_permission

    db_update_user_telegram_info(TEST_USER_ID, "perm_open_test", "Perm Open")
    # No permission row set for this user at all yet
    for perm_key in ("feedback", "requests", "team_create", "bot_access"):
        assert db_check_user_permission(TEST_USER_ID, perm_key) is True


# ---------- 9.2 / 9.3 / 9.4 / 9.5: blocking each permission independently ----------

def test_blocking_one_permission_does_not_affect_others(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_set_user_permission, db_check_user_permission

    db_update_user_telegram_info(TEST_USER_ID, "perm_isolated_test", "Perm Isolated")

    ok = db_set_user_permission(TEST_USER_ID, "feedback", False, set_by_admin="1")
    assert ok is True

    assert db_check_user_permission(TEST_USER_ID, "feedback") is False
    # everything else must remain unaffected
    assert db_check_user_permission(TEST_USER_ID, "requests") is True
    assert db_check_user_permission(TEST_USER_ID, "team_create") is True
    assert db_check_user_permission(TEST_USER_ID, "bot_access") is True


def test_all_four_permission_keys_independently_toggleable(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_set_user_permission, db_check_user_permission

    db_update_user_telegram_info(TEST_USER_ID, "perm_all_test", "Perm All")

    for perm_key in ("requests", "team_create", "bot_access"):
        db_set_user_permission(TEST_USER_ID, perm_key, False, set_by_admin="1")
        assert db_check_user_permission(TEST_USER_ID, perm_key) is False

    # re-enable one and confirm it flips back correctly, others stay blocked
    db_set_user_permission(TEST_USER_ID, "requests", True, set_by_admin="1")
    assert db_check_user_permission(TEST_USER_ID, "requests") is True
    assert db_check_user_permission(TEST_USER_ID, "team_create") is False
    assert db_check_user_permission(TEST_USER_ID, "bot_access") is False


def test_get_user_permissions_returns_all_four_keys_with_defaults(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_set_user_permission, db_get_user_permissions

    db_update_user_telegram_info(TEST_USER_ID, "perm_getall_test", "Perm GetAll")
    db_set_user_permission(TEST_USER_ID, "feedback", False, set_by_admin="1")

    perms = db_get_user_permissions(TEST_USER_ID)
    assert perms["feedback"] is False
    assert perms["requests"] is True   # default True, never explicitly set
    assert perms["team_create"] is True
    assert perms["bot_access"] is True