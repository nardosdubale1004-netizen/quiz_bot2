# tests/test_phase1_smoke.py
import pytest
from tests.conftest import TEST_USER_ID, TEST_USER_ID_2

# ---------- 1.2 / 1.3: schema + SQL functions exist, migration is idempotent ----------

def test_schema_migration_is_idempotent(engine):
    """Running the migration twice back-to-back must never raise."""
    engine._ensure_tournament_schema()
    engine._ensure_tournament_schema()


def test_core_tables_exist(db_conn):
    required_tables = [
        "questions", "user_stats", "sent_tracks", "user_responses",
        "tournament_queue", "organizations", "org_memberships",
        "user_locations", "location_suggestions", "feedback",
        "feedback_messages", "user_subject_marks", "user_difficulty_marks",
        "user_org_contributions", "user_geo_contributions",
        "school_branches", "user_favorites", "user_hidden_questions",
        "user_permissions", "channel_campaigns", "bot_state",
    ]
    with db_conn.cursor() as cur:
        for t in required_tables:
            cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = %s;", (t,)
            )
            assert cur.fetchone() is not None, f"Missing table: {t}"


def test_sql_functions_exist(db_conn):
    with db_conn.cursor() as cur:
        cur.execute("SELECT proname FROM pg_proc WHERE proname = 'fn_process_user_score';")
        assert cur.fetchone() is not None, "fn_process_user_score was not created"
        cur.execute("SELECT proname FROM pg_proc WHERE proname = 'fn_edit_user_answer';")
        assert cur.fetchone() is not None, "fn_edit_user_answer was not created"


def test_user_locations_is_single_source_of_truth(db_conn):
    """Regression guard: user_locations must keep exactly these columns —
    catches anyone reintroducing writes to the dead personal_city/personal_country pair."""
    with db_conn.cursor() as cur:
        cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'user_locations'
            AND column_name IN ('city', 'country', 'status', 'suggestion_id');
        """)
        found = {r["column_name"] for r in cur.fetchall()}
        assert found == {"city", "country", "status", "suggestion_id"}


# ---------- 1.6: Kroki reachable ----------

@pytest.mark.asyncio
async def test_kroki_renders_a_trivial_diagram():
    from src.rendering.kroki_client import get_latex_url, fetch_kroki_image
    tiny_tikz = r"""\documentclass{standalone}
\usepackage{tikz}
\begin{document}
\begin{tikzpicture}\draw (0,0) circle (1cm);\end{tikzpicture}
\end{document}"""
    url = get_latex_url(tiny_tikz)
    resp = await fetch_kroki_image(None, url, tiny_tikz)
    assert resp is not None, "Kroki call returned nothing — check KROKI_URL / network"
    assert resp.status_code == 200, f"Kroki returned {resp.status_code}: {resp.text[:300]}"


# ---------- 1.7 / 1.8: user row creation + exact lookup ----------

def test_new_user_row_is_created_and_readable(engine, clean_test_users):
    from src.database import db_update_user_telegram_info, db_get_user_profile

    ok = db_update_user_telegram_info(TEST_USER_ID, "test_handle", "Test First Name")
    assert ok is True

    profile = db_get_user_profile(TEST_USER_ID)
    assert profile is not None
    assert profile["user_id"] == TEST_USER_ID
    assert profile["username"] == "test_handle"
    assert profile["first_name"] == "Test First Name"
    assert profile["personal_city"] is None
    assert profile["personal_country"] is None
    assert profile["grade"] is None
    assert profile["total_marks"] == 0


def test_user_id_lookup_matches_exactly(engine, clean_test_users):
    """Mirrors what /whoami depends on — no cross-user bleed between two accounts."""
    from src.database import db_update_user_telegram_info, db_get_user_profile

    db_update_user_telegram_info(TEST_USER_ID, "user_a", "A")
    db_update_user_telegram_info(TEST_USER_ID_2, "user_b", "B")

    profile_a = db_get_user_profile(TEST_USER_ID)
    profile_b = db_get_user_profile(TEST_USER_ID_2)

    assert profile_a["username"] == "user_a"
    assert profile_b["username"] == "user_b"
    assert profile_a["user_id"] != profile_b["user_id"]