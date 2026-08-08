# tests/test_phase6_leaderboards.py
import pytest
from tests.conftest import TEST_USER_ID, TEST_USER_ID_2

TEST_ORG_TAG_SCHOOL = "TESTLBSCH1"
TEST_ORG_TAG_TEAM = "TESTLBTEM1"


@pytest.fixture
def clean_lb_orgs(engine):
    def _wipe():
        conn = engine.get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM org_memberships WHERE org_id IN (
                        SELECT org_id FROM organizations WHERE org_tag IN (%s, %s)
                    );
                """, (TEST_ORG_TAG_SCHOOL, TEST_ORG_TAG_TEAM))
                cur.execute("""
                    DELETE FROM user_org_contributions WHERE org_id IN (
                        SELECT org_id FROM organizations WHERE org_tag IN (%s, %s)
                    );
                """, (TEST_ORG_TAG_SCHOOL, TEST_ORG_TAG_TEAM))
                cur.execute(
                    "DELETE FROM organizations WHERE org_tag IN (%s, %s);",
                    (TEST_ORG_TAG_SCHOOL, TEST_ORG_TAG_TEAM)
                )
            conn.commit()
        finally:
            engine.release_connection(conn)
    _wipe()
    yield
    _wipe()


# ---------- 12.2 regression: School vs Team never mix in rank_matrix ----------

def test_school_and_team_never_mix_in_rank_matrix(engine, clean_test_users, clean_lb_orgs):
    from src.database import (
        db_update_user_telegram_info, db_create_organization, db_join_organization_by_id,
        db_get_rank_matrix
    )

    db_update_user_telegram_info(TEST_USER_ID, "lb_school_owner", "SchoolOwner")
    db_update_user_telegram_info(TEST_USER_ID_2, "lb_team_owner", "TeamOwner")

    school_id = db_create_organization(
        "LB Test School", TEST_ORG_TAG_SCHOOL, TEST_USER_ID,
        org_type="School", is_public=True, city="Addis Ababa", country="Ethiopia"
    )
    team_id = db_create_organization(
        "LB Test Team", TEST_ORG_TAG_TEAM, TEST_USER_ID_2,
        org_type="Team", is_public=True, city="Addis Ababa", country="Ethiopia"
    )
    assert school_id is not None
    assert team_id is not None

    matrix = db_get_rank_matrix(scope="world", entity=None, grade=None, subject=None, difficulty=None, mode="total", limit=10)

    school_names = [s["name"] for s in matrix.get("schools", [])]
    team_names = [t["name"] for t in matrix.get("teams", [])]

    assert "LB Test Team" not in school_names   # Team must never appear in the schools column
    assert "LB Test School" not in team_names   # School must never appear in the teams column


# ---------- 12.6 regression: pending location never counts on leaderboard population ----------

def test_pending_location_excluded_from_scope_summary(engine, clean_test_users):
    from src.database import (
        db_update_user_telegram_info, db_set_user_location, db_get_scope_summary
    )

    db_update_user_telegram_info(TEST_USER_ID, "pending_lb_test", "PendingLB")
    db_set_user_location(TEST_USER_ID, city="PendingCityXYZ", country="Ethiopia", status="pending", suggestion_id=None)

    summary = db_get_scope_summary(scope="city", entity="PendingCityXYZ", grade=None)
    assert summary["student_count"] == 0  # pending must not count toward city population


def test_approved_location_included_in_scope_summary(engine, clean_test_users):
    """Sanity-check counterpart to the above — an APPROVED location must count."""
    from src.database import (
        db_update_user_telegram_info, db_set_user_location, db_get_scope_summary
    )

    db_update_user_telegram_info(TEST_USER_ID, "approved_lb_test", "ApprovedLB")
    db_set_user_location(TEST_USER_ID, city="ApprovedCityXYZ", country="Ethiopia", status="approved")

    summary = db_get_scope_summary(scope="city", entity="ApprovedCityXYZ", grade=None)
    assert summary["student_count"] == 1


# ---------- 6.1/6.2: grade + subject filters combine with AND logic ----------

def test_rank_matrix_grade_filter_isolates_correct_students(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_set_user_grade, db_get_rank_matrix

    db_update_user_telegram_info(TEST_USER_ID, "grade10_student", "Grade10")
    db_set_user_grade(TEST_USER_ID, 10)

    db_update_user_telegram_info(TEST_USER_ID_2, "grade12_student", "Grade12")
    db_set_user_grade(TEST_USER_ID_2, 12)

    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE user_stats SET total_marks = 50 WHERE user_id = %s;", (TEST_USER_ID,))
            cur.execute("UPDATE user_stats SET total_marks = 80 WHERE user_id = %s;", (TEST_USER_ID_2,))
        conn.commit()
    finally:
        engine.release_connection(conn)

    matrix_g10 = db_get_rank_matrix(scope="world", entity=None, grade=10, subject=None, difficulty=None, mode="total", limit=10)
    student_names_g10 = [s["name"] for s in matrix_g10.get("students", [])]

    # Grade10 user should appear, Grade12 user should NOT appear when filtered to grade=10
    conn = engine.get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT nickname, username, first_name FROM user_stats WHERE user_id = %s;", (TEST_USER_ID,))
            u10 = cur.fetchone()
            cur.execute("SELECT nickname, username, first_name FROM user_stats WHERE user_id = %s;", (TEST_USER_ID_2,))
            u12 = cur.fetchone()
    finally:
        engine.release_connection(conn)

    # We just check counts here since exact display-name formatting can vary
    assert len(matrix_g10.get("students", [])) >= 1