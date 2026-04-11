import math

EMA_ALPHA = 0.15
DIFFICULTY_B = {
    1: -2.0,
    2: -1.0,
    3:  0.0,
    4:  1.0,
    5:  2.0,
}
TARGET_BAND = (0.70, 0.85)
MIN_ANSWERS_FOR_IRT = 5
DIFFICULTY_LABELS = {
    1: "Very Easy",
    2: "Easy",
    3: "Medium",
    4: "Hard",
    5: "Very Hard",
}

# ── Diagnostic constants ─────────────────────────────────────────────
DIAGNOSTIC_DIFFICULTIES    = [2, 3, 4]
DIAGNOSTIC_WEIGHTS         = {2: 0.15, 3: 0.25, 4: 0.35}
DIAGNOSTIC_BASE            = 0.10
DIAGNOSTIC_QUESTION_COUNT  = len(DIAGNOSTIC_DIFFICULTIES)
DIAGNOSTIC_UNTESTED_SHRINK = 0.92   # slight uncertainty penalty for untested topics
DIAGNOSTIC_DIRECT_WEIGHT   = 0.70   # weight for directly-tested topic evidence
DIAGNOSTIC_BASELINE_WEIGHT = 0.30   # complement


def compute_time_weight(time_ms: int) -> float:
    if time_ms < 8_000:   return 0.7
    if time_ms < 30_000:  return 1.0
    if time_ms < 60_000:  return 0.7
    return 0.5


def update_mastery(current_mastery: float, correct: bool, time_ms: int) -> float:
    time_weight = compute_time_weight(time_ms)
    raw_delta   = 0.05 * time_weight if correct else -0.04
    candidate   = current_mastery + raw_delta
    new_mastery = current_mastery * (1 - EMA_ALPHA) + candidate * EMA_ALPHA
    return round(max(0.0, min(1.0, new_mastery)), 4)


def mastery_to_theta(mastery: float) -> float:
    return (mastery - 0.5) * 8.0


def p_correct(theta: float, b: float) -> float:
    return 1.0 / (1.0 + math.exp(-(theta - b)))


def select_difficulty(mastery: float) -> int:
    theta        = mastery_to_theta(mastery)
    best_diff    = 2
    best_dist    = float("inf")
    band_center  = (TARGET_BAND[0] + TARGET_BAND[1]) / 2
    for difficulty, b in DIFFICULTY_B.items():
        p = p_correct(theta, b)
        d = abs(p - band_center)
        if d < best_dist:
            best_dist = d
            best_diff = difficulty
    return best_diff

def select_session_start_difficulty(topic_mastery: dict, session_topics: list[str]) -> int:
    """
    Pick the starting difficulty for a new session after diagnostic.
    Uses the weakest diagnosed topic in the selected session scope so the
    first real question is supportive, not overconfident.
    """
    if not session_topics:
        return 3

    scores = [topic_mastery.get(t, 0.5) for t in session_topics]
    if not scores:
        return 3

    anchor_mastery = min(scores)
    return select_difficulty(anchor_mastery)


def select_next_topic(topic_mastery: dict, mode: str = "auto") -> tuple:
    if mode == "weakness_review":
        return min(topic_mastery, key=topic_mastery.get), "weakness_review"
    if mode == "challenge":
        return max(topic_mastery, key=topic_mastery.get), "challenge"

    weakest       = min(topic_mastery, key=topic_mastery.get)
    weakest_score = topic_mastery[weakest]

    if weakest_score < 0.4:
        return weakest, "weakness_review"
    elif weakest_score < 0.7:
        return weakest, "normal_practice"
    else:
        return max(topic_mastery, key=topic_mastery.get), "challenge"


def get_initial_student_state(student_id: str, course_id: str, topics: list) -> dict:
    return {
        "student_id":                  student_id,
        "course_id":                   course_id,
        "topic_mastery":               {t: 0.5 for t in topics},
        "topic_mastery_source":        {t: "default_prior" for t in topics},
        "current_difficulty":          3,
        "total_answers":               0,
        "irt_active":                  False,
        "recent_answers":              [],
        "session_count":               0,
        "session_topics":              topics,
        "diagnostic_status_by_content": {},
        "last_updated":                None,
    }


def process_answer(state: dict, topic: str, correct: bool, time_ms: int) -> dict:
    current                       = state["topic_mastery"].get(topic, 0.5)
    state["topic_mastery"][topic] = update_mastery(current, correct, time_ms)

    # Track that this topic has been updated through real practice
    if "topic_mastery_source" not in state:
        state["topic_mastery_source"] = {}
    state["topic_mastery_source"][topic] = "practice_updated"

    state["total_answers"] += 1
    if state["total_answers"] >= MIN_ANSWERS_FOR_IRT:
        state["irt_active"] = True

    state["recent_answers"].append({
        "topic":   topic,
        "correct": correct,
        "time_ms": time_ms,
    })
    if len(state["recent_answers"]) > 20:
        state["recent_answers"].pop(0)

    if state["irt_active"]:
        state["current_difficulty"] = select_difficulty(state["topic_mastery"][topic])

    return state


# ── Diagnostic helpers ───────────────────────────────────────────────

def make_content_key(content_id: str, source_version: str) -> str:
    """
    Stable version-aware key for diagnostic tracking.
    content_id   = Mongo _id string of the content item
    source_version = uploaded_at ISO string (or hash) — invalidates on edit
    """
    return f"{content_id}:{source_version}"


def is_content_diagnosed(state: dict, content_key: str) -> bool:
    """True if the student has a completed diagnostic for this exact content version."""
    status = state.get("diagnostic_status_by_content", {}).get(content_key)
    return bool(status and status.get("completed"))


def get_diagnostic_difficulty(question_index: int) -> int:
    return DIAGNOSTIC_DIFFICULTIES[question_index % DIAGNOSTIC_QUESTION_COUNT]


def _compute_lecture_baseline(results: list[dict]) -> float:
    """
    Compute a single baseline mastery for the whole lecture from 3 diagnostic results.

    Scoring:
      0/3 correct → 0.10  (Struggling)
      easy only   → 0.25  (Emerging)
      med only    → 0.35  (Emerging)
      hard only   → 0.45  (Developing — knows hard, missed easier)
      easy+med    → 0.50  (Developing)
      easy+hard   → 0.60  (Developing)
      med+hard    → 0.70  (Proficient)
      all 3       → 0.85  (Proficient)
    """
    score = DIAGNOSTIC_BASE
    for r in results:
        if r.get("correct"):
            score += DIAGNOSTIC_WEIGHTS.get(int(r.get("difficulty", 3)), 0)
    return round(min(0.85, max(DIAGNOSTIC_BASE, score)), 4)


def apply_diagnostic_results(
    state:      dict,
    topics:     list[str],
    results:    list[dict],
    content_id: str,
    source_version: str,
) -> dict:
    """
    Dedicated diagnostic helper — completely separate from process_answer.

    For each topic in this content item:
      - If the topic was directly tested: blend direct score (70%) + baseline (30%)
      - If not tested: baseline × UNTESTED_SHRINK (slight uncertainty penalty)

    Also stamps topic_mastery_source = "diagnostic" for all affected topics.
    Does NOT touch recent_answers or total_answers — diagnostic is a separate
    evidence stream, not part of the adaptive practice history.
    """
    if "topic_mastery_source" not in state:
        state["topic_mastery_source"] = {}
    if "diagnostic_status_by_content" not in state:
        state["diagnostic_status_by_content"] = {}

    baseline = _compute_lecture_baseline(results)

    topic_masteries: dict[str, float] = {}

    for topic in topics:
        topic_results = [r for r in results if r.get("topic") == topic]

        if topic_results:
            direct_score = _compute_lecture_baseline(topic_results)
            blended      = round(
                DIAGNOSTIC_DIRECT_WEIGHT * direct_score +
                DIAGNOSTIC_BASELINE_WEIGHT * baseline,
                4,
            )
            topic_masteries[topic] = blended
        else:
            topic_masteries[topic] = round(baseline * DIAGNOSTIC_UNTESTED_SHRINK, 4)

    # Write to state — only overwrite if student has no practice history for this topic
    for topic, mastery in topic_masteries.items():
        current_source = state["topic_mastery_source"].get(topic, "default_prior")
        if current_source in ("default_prior", "diagnostic"):
            state["topic_mastery"][topic]        = mastery
            state["topic_mastery_source"][topic] = "diagnostic"

    # Record diagnostic completion for this content version
    content_key = make_content_key(content_id, source_version)
    state["diagnostic_status_by_content"][content_key] = {
        "completed":        True,
        "mastery":          baseline,
        "topic_masteries":  topic_masteries,
        "timestamp":        None,     # set by caller with UTC time
        "source_version":   source_version,
        "questions_count":  len(results),
    }

    return state