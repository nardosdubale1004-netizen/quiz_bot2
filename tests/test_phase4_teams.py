# tests/test_phase4_teams.py
import pytest
from tests.conftest import TEST_USER_ID, TEST_USER_ID_2

TEST_ORG_TAG_OPEN = "TESTOPEN01"
TEST_ORG_TAG_APPROVAL = "TESTAPPR01"
TEST_ORG_TAG_DEDICATED = "TESTDEDI01"


@pytest.fixture
def clean_test_orgs(engine):
    def _wipe():
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM org_memberships WHERE org_id IN (
                        SELECT org_id FROM organizations WHERE org_tag IN (%s, %s, %s)
                    );
                """, (TEST_ORG_TAG_OPEN, TEST_ORG_TAG_APPROVAL, TEST_ORG_TAG_DEDICATED))
                cur.execute("""
                    DELETE FROM user_org_contributions WHERE org_id IN (
                        SELECT org_id FROM organizations WHERE org_tag IN (%s, %s, %s)
                    );
                """, (TEST_ORG_TAG_OPEN, TEST_ORG_TAG_APPROVAL, TEST_ORG_TAG_DEDICATED))
                cur.execute(
                    "DELETE FROM organizations WHERE org_tag IN (%s, %s, %s);",
                    (TEST_ORG_TAG_OPEN, TEST_ORG_TAG_APPROVAL, TEST_ORG_TAG_DEDICATED)
                )
            conn.commit()
        finally:
            engine.release_connection(conn)
    _wipe()
    yield
    _wipe()


# ---------- 4.1: OPEN team -> instant active, no approval ----------

def test_open_team_join_is_instant(engine, clean_test_users, clean_test_orgs):
    from src.database import (
        db_update_user_telegram_info, db_create_organization, db_join_organization_by_id, db_get_user_org_role
    )

    db_update_user_telegram_info(TEST_USER_ID, "creator_open", "Creator")
    db_update_user_telegram_info(TEST_USER_ID_2, "joiner_open", "Joiner")

    org_id = db_create_organization(
        "Open Test Team", TEST_ORG_TAG_OPEN, TEST_USER_ID,
        org_type="Team", is_public=True, city="Addis Ababa", country="Ethiopia"
    )
    assert org_id is not None

    join_data = db_join_organization_by_id(TEST_USER_ID_2, org_id)
    assert join_data is not None
    assert join_data["role_assigned"] == "active"  # instant, no pending

    role = db_get_user_org_role(TEST_USER_ID_2, org_id)
    assert role == "member"


# ---------- 4.2: APPROVAL REQUIRED team -> pending until admin approves ----------

def test_approval_required_team_join_is_pending(engine, clean_test_users, clean_test_orgs):
    from src.database import (
        db_update_user_telegram_info, db_create_organization, db_join_organization_by_id,
        db_get_user_org_role, db_approve_member_request
    )

    db_update_user_telegram_info(TEST_USER_ID, "creator_appr", "Creator")
    db_update_user_telegram_info(TEST_USER_ID_2, "joiner_appr", "Joiner")

    org_id = db_create_organization(
        "Approval Test Team", TEST_ORG_TAG_APPROVAL, TEST_USER_ID,
        org_type="Team", is_public=False, city="Addis Ababa", country="Ethiopia"
    )
    assert org_id is not None

    join_data = db_join_organization_by_id(TEST_USER_ID_2, org_id)
    assert join_data is not None
    assert join_data["role_assigned"] == "pending"

    # Not active yet — role lookup (which filters state='active') should return None
    role = db_get_user_org_role(TEST_USER_ID_2, org_id)
    assert role is None

    # Admin approves
    ok = db_approve_member_request(TEST_USER_ID_2, org_id, approve=True)
    assert ok is True

    role_after = db_get_user_org_role(TEST_USER_ID_2, org_id)
    assert role_after == "member"


# ---------- 4.3: dedicated (scoped) team blocks mismatched users ----------

def test_dedicated_team_blocks_wrong_city_user(engine, clean_test_users, clean_test_orgs):
    from src.database import (
        db_update_user_telegram_info, db_set_user_location, db_create_dedicated_organization,
        db_join_organization_by_id
    )

    db_update_user_telegram_info(TEST_USER_ID, "creator_dedi", "Creator")
    db_set_user_location(TEST_USER_ID, city="Addis Ababa", country="Ethiopia", status="approved")

    db_update_user_telegram_info(TEST_USER_ID_2, "wrong_city_user", "WrongCity")
    db_set_user_location(TEST_USER_ID_2, city="Nairobi", country="Kenya", status="approved")

    org_id = db_create_dedicated_organization(
        "Addis Only Team", TEST_ORG_TAG_DEDICATED, TEST_USER_ID,
        team_scope="city", scope_value="Addis Ababa", description="Addis-only squad",
        city="Addis Ababa", country="Ethiopia", is_public=True
    )
    assert org_id is not None

    join_data = db_join_organization_by_id(TEST_USER_ID_2, org_id)
    assert join_data is not None
    assert join_data.get("scope_blocked") is True
    assert "Addis Ababa" in join_data["reason"]


def test_dedicated_team_allows_matching_city_user(engine, clean_test_users, clean_test_orgs):
    from src.database import (
        db_update_user_telegram_info, db_set_user_location, db_create_dedicated_organization,
        db_join_organization_by_id, db_get_user_org_role
    )

    db_update_user_telegram_info(TEST_USER_ID, "creator_dedi2", "Creator")
    db_set_user_location(TEST_USER_ID, city="Addis Ababa", country="Ethiopia", status="approved")

    db_update_user_telegram_info(TEST_USER_ID_2, "right_city_user", "RightCity")
    db_set_user_location(TEST_USER_ID_2, city="Addis Ababa", country="Ethiopia", status="approved")

    org_id = db_create_dedicated_organization(
        "Addis Only Team 2", TEST_ORG_TAG_DEDICATED, TEST_USER_ID,
        team_scope="city", scope_value="Addis Ababa", description="Addis-only squad",
        city="Addis Ababa", country="Ethiopia", is_public=True
    )
    assert org_id is not None

    join_data = db_join_organization_by_id(TEST_USER_ID_2, org_id)
    assert join_data is not None
    assert join_data.get("scope_blocked") is not True
    assert join_data["role_assigned"] == "active"

    role = db_get_user_org_role(TEST_USER_ID_2, org_id)
    assert role == "member"


# ---------- 4.4: school join is ALWAYS instant, regardless of is_public ----------

def test_school_join_always_instant_even_if_is_public_false(engine, clean_test_users, clean_test_orgs):
    from src.database import (
        db_update_user_telegram_info, db_create_organization, db_join_organization_by_id
    )

    db_update_user_telegram_info(TEST_USER_ID, "creator_school", "Creator")
    db_update_user_telegram_info(TEST_USER_ID_2, "joiner_school", "Joiner")

    # School with is_public=False — schools must NEVER gate on approval, only teams do
    org_id = db_create_organization(
        "Approval-Style School", TEST_ORG_TAG_APPROVAL, TEST_USER_ID,
        org_type="School", is_public=False, city="Addis Ababa", country="Ethiopia"
    )
    assert org_id is not None

    join_data = db_join_organization_by_id(TEST_USER_ID_2, org_id)
    assert join_data is not None
    assert join_data["role_assigned"] == "active"  # instant despite is_public=False


# ---------- 4.6: leaving a team never changes personal total_marks ----------

def test_leaving_team_preserves_personal_marks(engine, clean_test_users, clean_test_orgs):
    from src.database import (
        db_update_user_telegram_info, db_create_organization, db_join_organization_by_id,
        db_get_user_profile, db_leave_organization
    )

    db_update_user_telegram_info(TEST_USER_ID, "creator_leave", "Creator")
    db_update_user_telegram_info(TEST_USER_ID_2, "leaver", "Leaver")

    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_stats SET total_marks = 250 WHERE user_id = %s;", (TEST_USER_ID_2,))
        conn.commit()
    finally:
        engine.release_connection(conn)

    org_id = db_create_organization(
        "Leave Test Team", TEST_ORG_TAG_OPEN, TEST_USER_ID,
        org_type="Team", is_public=True, city="Addis Ababa", country="Ethiopia"
    )
    db_join_organization_by_id(TEST_USER_ID_2, org_id)

    result = db_leave_organization(TEST_USER_ID_2, org_id)
    assert result is not None

    profile = db_get_user_profile(TEST_USER_ID_2)
    assert profile["total_marks"] == 250  # untouched by leaving
    assert profile["team_id"] is None  # no longer shows as active team member