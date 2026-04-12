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

ALLOWED_MODES = {"auto", "normal_practice", "weakness_review", "challenge"}

WEAK_THRESHOLD = 0.45
GROWTH_MAX = 0.70

ROTATION_POOL_SIZE = 3
WEAK_CHECKIN_CADENCE = 4
SPACED_REVIEW_CADENCE = 5

# ── Diagnostic constants ─────────────────────────────────────────────
DIAGNOSTIC_MIN_QUESTIONS = 5
DIAGNOSTIC_MAX_QUESTIONS = 10
DIAGNOSTIC_UNTESTED_SHRINK = 0.94

DIAGNOSTIC_ONE_EVIDENCE_DIRECT_WEIGHT = 0.55
DIAGNOSTIC_MULTI_EVIDENCE_DIRECT_WEIGHT = 0.75

DIAGNOSTIC_DIFF_SCORE_WEIGHTS = {
    2: 1.00,   # easy
    3: 1.15,   # medium
    4: 1.30,   # hard
}

DIAGNOSTIC_BASE_FLOOR = 0.15
DIAGNOSTIC_BASE_CEILING = 0.85


def _normalize_time_context(time_context: str | None) -> str | None:
    if time_context in {"thinking", "distracted", "unknown"}:
        return time_context
    return None


def compute_time_weight(time_ms: int, time_context: str | None = None) -> float:
    time_context = _normalize_time_context(time_context)
    if time_context == "distracted":
        return 1.0
    if time_ms < 8_000:   return 0.7
    if time_ms < 30_000:  return 1.0
    if time_ms < 60_000:  return 0.7
    return 0.5


def update_mastery(current_mastery: float, correct: bool, time_ms: int, time_context: str | None = None) -> float:
    time_weight = compute_time_weight(time_ms, time_context=time_context)
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

def select_session_start_difficulty(
    topic_mastery: dict,
    session_topics: list[str],
    mode: str = "normal_practice",
) -> int:
    if not session_topics:
        return 3

    scoped = {t: topic_mastery.get(t, 0.5) for t in session_topics}
    if not scoped:
        return 3

    mode = _normalize_mode(mode)
    if mode == "auto":
        mode = resolve_auto_mode(scoped)

    scores = sorted(scoped.values())
    weak_scores = [s for s in scores if s < WEAK_THRESHOLD]
    growth_scores = [s for s in scores if WEAK_THRESHOLD <= s <= GROWTH_MAX]
    strong_scores = [s for s in scores if s > GROWTH_MAX]

    if mode == "weakness_review":
        anchor_mastery = min(scores)
    elif mode == "challenge":
        anchor_mastery = max(scores)
    else:
        # normal_practice anchor = frontier topic
        if growth_scores:
            anchor_mastery = growth_scores[0]
        elif weak_scores:
            anchor_mastery = max(weak_scores)
        elif strong_scores:
            anchor_mastery = min(strong_scores)
        else:
            anchor_mastery = 0.5

    return select_difficulty(anchor_mastery)


def select_next_topic(
    topic_mastery: dict,
    mode: str = "normal_practice",
    recent_answers: list[dict] | None = None,
    total_answers: int = 0,
) -> tuple:
    if not topic_mastery:
        raise ValueError("topic_mastery cannot be empty.")

    recent_answers = recent_answers or []
    mode = _normalize_mode(mode)

    if mode == "auto":
        mode = resolve_auto_mode(topic_mastery, recent_answers)

    ordered_low = [t for t, _ in sorted(topic_mastery.items(), key=lambda x: x[1])]
    ordered_high = [t for t, _ in sorted(topic_mastery.items(), key=lambda x: x[1], reverse=True)]

    weak_topics = [t for t in ordered_low if topic_mastery[t] < WEAK_THRESHOLD]
    growth_topics = [t for t in ordered_low if WEAK_THRESHOLD <= topic_mastery[t] <= GROWTH_MAX]
    strong_topics = [t for t in ordered_low if topic_mastery[t] > GROWTH_MAX]

    if mode == "weakness_review":
        candidates = ordered_low[:min(ROTATION_POOL_SIZE, len(ordered_low))]
        for topic in _recent_wrong_topics(recent_answers):
            if topic in topic_mastery and topic not in candidates and topic_mastery[topic] <= 0.65:
                candidates.append(topic)

        chosen = _pick_rotating_topic(candidates, topic_mastery, recent_answers)
        return chosen, "weakness_review"

    if mode == "challenge":
        candidates = ordered_high[:min(ROTATION_POOL_SIZE, len(ordered_high))]
        chosen = _pick_rotating_topic(
            candidates,
            topic_mastery,
            recent_answers,
            prefer_high_mastery=True,
        )
        return chosen, "challenge"

    # normal_practice
    if growth_topics:
        if strong_topics and total_answers > 0 and total_answers % SPACED_REVIEW_CADENCE == 0:
            candidates = strong_topics[:min(ROTATION_POOL_SIZE, len(strong_topics))]
            chosen = _pick_rotating_topic(candidates, topic_mastery, recent_answers)
            return chosen, "normal_practice"

        if weak_topics and total_answers > 0 and total_answers % WEAK_CHECKIN_CADENCE == 0:
            candidates = weak_topics[:min(ROTATION_POOL_SIZE, len(weak_topics))]
            chosen = _pick_rotating_topic(candidates, topic_mastery, recent_answers)
            return chosen, "normal_practice"

        candidates = growth_topics[:min(ROTATION_POOL_SIZE, len(growth_topics))]
        chosen = _pick_rotating_topic(candidates, topic_mastery, recent_answers)
        return chosen, "normal_practice"

    if weak_topics:
        # fallback: learner is mostly weak, so use the strongest weak topic as the frontier
        candidates = weak_topics[:min(ROTATION_POOL_SIZE, len(weak_topics))]
        chosen = _pick_rotating_topic(candidates, topic_mastery, recent_answers)
        return chosen, "normal_practice"

    # fallback: learner is already strong everywhere, so use lowest strong topic as productive practice
    candidates = strong_topics[:min(ROTATION_POOL_SIZE, len(strong_topics))] or ordered_low[:1]
    chosen = _pick_rotating_topic(candidates, topic_mastery, recent_answers)
    return chosen, "normal_practice"


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
        "current_session_mode": "normal_practice",
    }


def process_answer(
    state: dict,
    topic: str,
    correct: bool,
    time_ms: int,
    time_context: str | None = None,
) -> dict:
    current                       = state["topic_mastery"].get(topic, 0.5)
    state["topic_mastery"][topic] = update_mastery(current, correct, time_ms, time_context=time_context)

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
        "time_context": _normalize_time_context(time_context) or "unknown",
    })
    if len(state["recent_answers"]) > 20:
        state["recent_answers"].pop(0)

    if state["irt_active"]:
        state["current_difficulty"] = select_difficulty(state["topic_mastery"][topic])

    return state

def _normalize_mode(mode: str) -> str:
    return mode if mode in ALLOWED_MODES else "normal_practice"


def _last_seen_distance(topic: str, recent_answers: list[dict] | None) -> int:
    recent_answers = recent_answers or []
    for i, ans in enumerate(reversed(recent_answers), start=1):
        if ans.get("topic") == topic:
            return i
    return 10_000


def _pick_rotating_topic(
    candidates: list[str],
    topic_mastery: dict[str, float],
    recent_answers: list[dict] | None = None,
    prefer_high_mastery: bool = False,
) -> str:
    recent_answers = recent_answers or []
    if not candidates:
        raise ValueError("No candidate topics available.")

    last_topic = recent_answers[-1].get("topic") if recent_answers else None
    pool = [t for t in candidates if t != last_topic] or list(candidates)

    if prefer_high_mastery:
        return sorted(
            pool,
            key=lambda t: (
                -_last_seen_distance(t, recent_answers),
                -topic_mastery.get(t, 0.5),
                t,
            )
        )[0]

    return sorted(
        pool,
        key=lambda t: (
            -_last_seen_distance(t, recent_answers),
            topic_mastery.get(t, 0.5),
            t,
        )
    )[0]


def _recent_wrong_topics(recent_answers: list[dict] | None, limit: int = 6) -> list[str]:
    recent_answers = recent_answers or []
    wrong_topics = []
    for ans in reversed(recent_answers[-limit:]):
        topic = ans.get("topic")
        if not ans.get("correct") and topic and topic not in wrong_topics:
            wrong_topics.append(topic)
    return wrong_topics


def resolve_auto_mode(topic_mastery: dict[str, float], recent_answers: list[dict] | None = None) -> str:
    recent_answers = recent_answers or []
    weakest_score = min(topic_mastery.values()) if topic_mastery else 0.5

    if len(recent_answers) >= 2 and all(not ans.get("correct") for ans in recent_answers[-2:]):
        return "weakness_review"

    if weakest_score < 0.35:
        return "weakness_review"

    if any(WEAK_THRESHOLD <= score <= GROWTH_MAX for score in topic_mastery.values()):
        return "normal_practice"

    return "challenge"


# ── Diagnostic helpers ───────────────────────────────────────────────

def diagnostic_target_question_count(num_topics: int) -> int:
    """
    Variable-length lecture readiness check.

    1–2 topics  -> 5 questions
    3–4 topics  -> 6 questions
    5–6 topics  -> 8 questions
    7+ topics   -> 10 questions
    """
    if num_topics <= 2:
        return 5
    if num_topics <= 4:
        return 6
    if num_topics <= 6:
        return 8
    return 10


def diagnostic_coverage_goal(num_topics: int, target_questions: int | None = None) -> int:
    """
    Reserve at least some room for refinement after broad coverage.
    """
    target = target_questions or diagnostic_target_question_count(num_topics)
    reserve_refinement = 2 if target >= 7 else 1
    return min(num_topics, max(1, target - reserve_refinement))


def diagnostic_band_label(mastery: float) -> str:
    if mastery < WEAK_THRESHOLD:
        return "weak"
    if mastery <= GROWTH_MAX:
        return "growth_ready"
    return "strong"


def _diagnostic_weighted_accuracy(results: list[dict]) -> float:
    if not results:
        return 0.5

    total_weight = 0.0
    earned_weight = 0.0

    for r in results:
        diff = int(r.get("difficulty", 3))
        weight = DIAGNOSTIC_DIFF_SCORE_WEIGHTS.get(diff, 1.15)
        total_weight += weight
        if r.get("correct"):
            earned_weight += weight

    if total_weight <= 0:
        return 0.5

    return earned_weight / total_weight


def _compute_lecture_baseline(results: list[dict]) -> float:
    """
    Variable-length readiness baseline.

    This is now a weighted proportion of correct answers, not a fixed 3-question
    sum, so it scales properly for 5/6/8/10-question diagnostics.
    """
    acc = _diagnostic_weighted_accuracy(results)
    mastery = DIAGNOSTIC_BASE_FLOOR + (acc * (DIAGNOSTIC_BASE_CEILING - DIAGNOSTIC_BASE_FLOOR))
    return round(max(DIAGNOSTIC_BASE_FLOOR, min(DIAGNOSTIC_BASE_CEILING, mastery)), 4)


def _build_diagnostic_topic_preview(topics: list[str], results: list[dict]) -> tuple[float, dict[str, dict]]:
    """
    Returns:
      - lecture baseline
      - per-topic preview with evidence count, uncertainty, and provisional mastery
    """
    baseline = _compute_lecture_baseline(results)
    preview: dict[str, dict] = {}

    for topic in topics:
        topic_results = [r for r in results if r.get("topic") == topic]
        evidence_count = len(topic_results)

        if evidence_count == 0:
            direct_mastery = None
            mastery_estimate = round(baseline * DIAGNOSTIC_UNTESTED_SHRINK, 4)
            uncertainty = 1.0
            mixed_outcomes = False
        else:
            direct_mastery = _compute_lecture_baseline(topic_results)

            direct_weight = (
                DIAGNOSTIC_ONE_EVIDENCE_DIRECT_WEIGHT
                if evidence_count == 1
                else DIAGNOSTIC_MULTI_EVIDENCE_DIRECT_WEIGHT
            )

            mastery_estimate = round(
                direct_weight * direct_mastery +
                (1.0 - direct_weight) * baseline,
                4,
            )

            mixed_outcomes = len({bool(r.get("correct")) for r in topic_results}) > 1

            if evidence_count == 1:
                uncertainty = 0.55
            else:
                uncertainty = 0.35

            if mixed_outcomes:
                uncertainty = min(1.0, uncertainty + 0.15)

        preview[topic] = {
            "evidence_count": evidence_count,
            "direct_mastery": direct_mastery,
            "mastery_estimate": mastery_estimate,
            "uncertainty": round(uncertainty, 4),
            "mixed_outcomes": mixed_outcomes,
            "band": diagnostic_band_label(mastery_estimate),
        }

    return baseline, preview


def plan_diagnostic_question(
    topics: list[str],
    results_so_far: list[dict],
    question_index: int,
    target_questions: int | None = None,
) -> dict:
    """
    Coverage first, refinement second.

    Stage 1:
      - broadly cover the lecture topics
    Stage 2:
      - spend remaining questions on the most uncertain topics
    """
    if not topics:
        raise ValueError("topics cannot be empty for diagnostic planning.")

    target = target_questions or diagnostic_target_question_count(len(topics))
    coverage_goal = diagnostic_coverage_goal(len(topics), target)

    baseline, preview = _build_diagnostic_topic_preview(topics, results_so_far)
    asked_count = len(results_so_far)
    last_topic = results_so_far[-1].get("topic") if results_so_far else None

    # ── Stage 1: coverage ──────────────────────────────────────────
    if asked_count < coverage_goal:
        coverage_topics = topics[:coverage_goal]
        uncovered = [t for t in coverage_topics if preview[t]["evidence_count"] == 0]
        candidates = uncovered or coverage_topics
        next_topic = next((t for t in candidates if t != last_topic), candidates[0])

        coverage_index = asked_count
        if coverage_index == 0:
            difficulty = 2
        elif coverage_index == coverage_goal - 1 and coverage_goal >= 4:
            difficulty = 4
        else:
            difficulty = 3

        return {
            "topic": next_topic,
            "difficulty": difficulty,
            "target_questions": target,
            "coverage_goal": coverage_goal,
            "phase": "coverage",
            "lecture_baseline_preview": baseline,
        }

    # ── Stage 2: refinement ────────────────────────────────────────
    ranked_topics = sorted(
        topics,
        key=lambda t: (
            -preview[t]["uncertainty"],           # most uncertain first
            preview[t]["evidence_count"],         # lower evidence first
            abs((preview[t]["mastery_estimate"] or baseline) - 0.5),  # closer to middle = more ambiguous
        )
    )

    candidates = [t for t in ranked_topics if t != last_topic] or ranked_topics
    next_topic = candidates[0]

    est = preview[next_topic]["mastery_estimate"]
    evidence = preview[next_topic]["evidence_count"]

    if evidence == 0:
        difficulty = 3
    elif est < WEAK_THRESHOLD:
        difficulty = 2
    elif est > GROWTH_MAX:
        difficulty = 4
    else:
        difficulty = 3

    return {
        "topic": next_topic,
        "difficulty": difficulty,
        "target_questions": target,
        "coverage_goal": coverage_goal,
        "phase": "refinement",
        "lecture_baseline_preview": baseline,
    }

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


def apply_diagnostic_results(
    state: dict,
    topics: list[str],
    results: list[dict],
    content_id: str,
    source_version: str,
) -> dict:
    """
    Final diagnostic writeback.

    This now stores:
      - lecture baseline
      - topic mastery estimates
      - topic evidence counts
      - topic uncertainty
      - provisional band labels
    """
    if "topic_mastery_source" not in state:
        state["topic_mastery_source"] = {}
    if "diagnostic_status_by_content" not in state:
        state["diagnostic_status_by_content"] = {}

    target_questions = diagnostic_target_question_count(len(topics))
    coverage_goal = diagnostic_coverage_goal(len(topics), target_questions)

    baseline, preview = _build_diagnostic_topic_preview(topics, results)

    topic_masteries = {
        topic: preview[topic]["mastery_estimate"]
        for topic in topics
    }
    topic_evidence_counts = {
        topic: preview[topic]["evidence_count"]
        for topic in topics
    }
    topic_uncertainty = {
        topic: preview[topic]["uncertainty"]
        for topic in topics
    }
    topic_provisional_bands = {
        topic: preview[topic]["band"]
        for topic in topics
    }

    for topic, mastery in topic_masteries.items():
        current_source = state["topic_mastery_source"].get(topic, "default_prior")
        if current_source in ("default_prior", "diagnostic"):
            state["topic_mastery"][topic] = mastery
            state["topic_mastery_source"][topic] = "diagnostic"

    content_key = make_content_key(content_id, source_version)
    state["diagnostic_status_by_content"][content_key] = {
        "completed": True,
        "mastery": baseline,
        "topic_masteries": topic_masteries,
        "topic_evidence_counts": topic_evidence_counts,
        "topic_uncertainty": topic_uncertainty,
        "topic_provisional_bands": topic_provisional_bands,
        "timestamp": None,
        "source_version": source_version,
        "questions_count": len(results),
        "expected_questions": target_questions,
        "coverage_goal": coverage_goal,
    }

    return state
