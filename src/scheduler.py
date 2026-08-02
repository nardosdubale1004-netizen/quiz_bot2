# src/scheduler.py
"""
Diversified question scheduling engine.

Pipeline: hard cooldown filter (done in SQL via db_get_scheduling_pool) ->
score every remaining candidate on novelty + diversity + balance + curriculum
priority -> weighted-random pick among the top-K scored candidates (not pure
greedy — keeps the feed feeling alive rather than mechanically predictable).
"""
import random
from datetime import datetime, timezone


def _days_since(dt):
    if not dt:
        return 999.0
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return 999.0
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def _normalize_difficulty(d):
    d = (d or "medium").lower()
    return "easy" if d == "weak" else d


def score_candidates(pool, recent_history, subject_weights=None, difficulty_target=None,
                      topic_boosts=None, last_topics=None):
    """
    pool: list of question dicts (already cooldown-filtered)
    recent_history: list of {subject, difficulty, topic} dicts, most recent first
    subject_weights: dict subject_lower -> target share (0-1). Omit for equal weighting.
    difficulty_target: dict {'easy':0.4,'medium':0.4,'hard':0.2}
    topic_boosts: dict topic -> multiplier (admin curriculum priority, e.g. exam prep)
    last_topics: topics from the last 1-2 posts (soft lockout, avoids back-to-back repeats)
    Returns: list of (score, question) sorted descending.
    """
    subject_weights = subject_weights or {}
    difficulty_target = difficulty_target or {"easy": 0.4, "medium": 0.4, "hard": 0.2}
    topic_boosts = topic_boosts or {}
    last_topics = last_topics or []

    total_recent = len(recent_history) or 1
    subj_counts, diff_counts = {}, {}
    for h in recent_history:
        s = (h.get("subject") or "").lower()
        d = _normalize_difficulty(h.get("difficulty"))
        subj_counts[s] = subj_counts.get(s, 0) + 1
        diff_counts[d] = diff_counts.get(d, 0) + 1

    default_subj_share = 1.0 / max(1, len(subject_weights) or 1)

    scored = []
    for q in pool:
        subject = (q.get("subject") or "").lower()
        difficulty = _normalize_difficulty(q.get("difficulty"))
        topic = q.get("topic") or ""

        novelty = min(_days_since(q.get("last_shown_at")), 60) / 60.0

        subj_ratio = subj_counts.get(subject, 0) / total_recent
        target_subj = subject_weights.get(subject, default_subj_share)
        diversity_bonus = max(0.0, target_subj - subj_ratio) * 2.0

        diff_ratio = diff_counts.get(difficulty, 0) / total_recent
        target_diff = difficulty_target.get(difficulty, 0.33)
        balance_bonus = max(0.0, target_diff - diff_ratio) * 2.0

        curriculum_mult = topic_boosts.get(topic, 1.0)
        lockout_penalty = 0.5 if topic in last_topics else 0.0

        base = (0.35 * novelty) + (0.30 * diversity_bonus) + (0.25 * balance_bonus)
        base *= curriculum_mult
        base = max(0.01, base - lockout_penalty)

        scored.append((base, q))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def select_batch(pool, recent_history, n=5, subject_weights=None, difficulty_target=None,
                  topic_boosts=None, top_k=5):
    """Selects n questions, re-scoring after each pick so consecutive picks stay diversified
    against each other, not just against history."""
    working_pool = list(pool)
    working_history = list(recent_history)
    selected, last_topics = [], []

    for _ in range(n):
        if not working_pool:
            break
        scored = score_candidates(
            working_pool, working_history,
            subject_weights=subject_weights,
            difficulty_target=difficulty_target,
            topic_boosts=topic_boosts,
            last_topics=last_topics[-2:]
        )
        top = scored[:top_k]
        weights = [s for s, _ in top]
        total_w = sum(weights) or 1.0
        r = random.uniform(0, total_w)
        upto, chosen = 0.0, top[0][1]
        for w, q in top:
            upto += w
            if upto >= r:
                chosen = q
                break

        selected.append(chosen)
        working_pool = [q for q in working_pool if q["id"] != chosen["id"]]
        working_history.insert(0, {
            "subject": chosen.get("subject"),
            "difficulty": chosen.get("difficulty"),
            "topic": chosen.get("topic"),
        })
        last_topics.append(chosen.get("topic"))

    return selected