import math

EMA_ALPHA = 0.15
DIFFICULTY_B = {1: -1.5, 2: 0.0, 3: 1.5}
TARGET_BAND = (0.70, 0.85)
MIN_ANSWERS_FOR_IRT = 5


def compute_time_weight(time_ms: int) -> float:
    if time_ms < 8_000:   return 0.7
    if time_ms < 30_000:  return 1.0
    if time_ms < 60_000:  return 0.7
    return 0.5


def update_mastery(current_mastery: float, correct: bool, time_ms: int) -> float:
    time_weight = compute_time_weight(time_ms)
    raw_delta = 0.05 * time_weight if correct else -0.04
    candidate = current_mastery + raw_delta
    new_mastery = current_mastery * (1 - EMA_ALPHA) + candidate * EMA_ALPHA
    return round(max(0.0, min(1.0, new_mastery)), 4)


def mastery_to_theta(mastery: float) -> float:
    return (mastery - 0.5) * 4.0


def p_correct(theta: float, b: float) -> float:
    return 1.0 / (1.0 + math.exp(-(theta - b)))


def select_difficulty(mastery: float) -> int:
    theta = mastery_to_theta(mastery)
    best_difficulty = 2
    best_distance = float("inf")
    band_center = (TARGET_BAND[0] + TARGET_BAND[1]) / 2

    for difficulty, b in DIFFICULTY_B.items():
        p = p_correct(theta, b)
        distance = abs(p - band_center)
        if distance < best_distance:
            best_distance = distance
            best_difficulty = difficulty

    return best_difficulty


def select_next_topic(topic_mastery: dict, mode: str = "auto") -> tuple:
    if mode == "weakness_review":
        topic = min(topic_mastery, key=topic_mastery.get)
        return topic, "weakness_review"

    if mode == "challenge":
        topic = max(topic_mastery, key=topic_mastery.get)
        return topic, "challenge"

    weakest = min(topic_mastery, key=topic_mastery.get)
    weakest_score = topic_mastery[weakest]

    if weakest_score < 0.4:
        return weakest, "weakness_review"
    elif weakest_score < 0.7:
        return weakest, "normal_practice"
    else:
        strongest = max(topic_mastery, key=topic_mastery.get)
        return strongest, "challenge"


def get_initial_student_state(student_id: str, course_id: str, topics: list) -> dict:
    return {
        "student_id": student_id,
        "course_id": course_id,
        "topic_mastery": {t: 0.5 for t in topics},
        "current_difficulty": 2,
        "total_answers": 0,
        "irt_active": False,
        "recent_answers": [],
        "session_count": 0,
        "session_topics": topics,
        "last_updated": None
    }


def process_answer(state: dict, topic: str, correct: bool, time_ms: int) -> dict:
    # Update mastery for this topic
    current = state["topic_mastery"].get(topic, 0.5)
    state["topic_mastery"][topic] = update_mastery(current, correct, time_ms)

    # Update answer count and activate IRT after threshold
    state["total_answers"] += 1
    if state["total_answers"] >= MIN_ANSWERS_FOR_IRT:
        state["irt_active"] = True

    # Add to rolling window (max 20)
    state["recent_answers"].append({
        "topic": topic,
        "correct": correct,
        "time_ms": time_ms
    })
    if len(state["recent_answers"]) > 20:
        state["recent_answers"].pop(0)

    # Update difficulty using IRT if active, else keep at 2
    if state["irt_active"]:
        state["current_difficulty"] = select_difficulty(state["topic_mastery"][topic])

    return state