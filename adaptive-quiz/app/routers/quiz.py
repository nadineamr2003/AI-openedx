from fastapi import APIRouter, HTTPException
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import hashlib
import json
import logging
from bson import ObjectId
from app.models.quiz import (
    GenerateRequest, SubmitRequest, SubmitResponse, MasteryResponse,
    ContentItem, ContentListResponse, ContentUpdateRequest, ContentToggleRequest,
    DiagnosticGenerateRequest, DiagnosticCompleteRequest,
    DiagnosticItem, SessionFinalizeRequest, RecoveryStartRequest,
    RecoveryDeclineRequest, RecoverySubmitRequest,
)
from app.services.adaptation import (
    get_initial_student_state,
    process_answer,
    normalize_confidence_level,
    select_next_topic,
    select_difficulty,
    make_content_key,
    is_content_diagnosed,
    apply_diagnostic_results,
    diagnostic_target_question_count,
    diagnostic_coverage_goal,
    plan_diagnostic_question,
    select_session_start_difficulty,
    TARGET_BAND,
)
from app.services.ai_engine import (
    generate_question,
    generate_question_with_metadata,
    generate_simple_explanation,
    extract_content_metadata,
)
from app.services.pdf_parser import extract_text_from_pdf_base64
from app.db.mongodb import get_db
import asyncio

router = APIRouter(prefix="/api/quiz", tags=["quiz"])
CACHE_PROMPT_VERSION = "quiz-cache-v2"
CACHE_UNUSED_TTL_DAYS = 14
CACHE_USED_TTL_DAYS = 7
DEFAULT_CACHE_TARGET = 1
FOCUSED_FOLLOWUP_CACHE_TARGET = 2
FOCUSED_FOLLOWUP_WAIT_SECONDS = 2.0
FOCUSED_FOLLOWUP_WAIT_INTERVAL_SECONDS = 0.25
SESSION_ORIGIN_STANDARD = "standard"
SESSION_ORIGIN_FOLLOWUP = "followup"
logger = logging.getLogger(__name__)
_REPLENISH_IN_FLIGHT: set[str] = set()
RECENT_QUESTION_HISTORY_LIMIT = 40
RECENT_QUESTION_HISTORY_MAX_AGE = timedelta(days=14)
LIVE_DEDUPE_MAX_ATTEMPTS = 3
CHALLENGE_READY_AVG_MASTERY = TARGET_BAND[0]
CHALLENGE_NOT_READY_ERROR = "challenge_not_ready"
DIAGNOSTIC_FAST_PATH_MAX_ATTEMPTS = 3
RECOVERY_SUPPORT_MESSAGE = (
    "You seem to be struggling with this concept. I can simplify it and give you one focused "
    "recovery question before we continue."
)
RECOVERY_THOUGHTFUL_RESPONSE_MS = 90_000
RECOVERY_REASON_THOUGHTFUL = "thoughtful_wrong_answer"
RECOVERY_REASON_REPEATED = "repeated_wrong_topic"
RECOVERY_REVIEW_PRESSURE_THRESHOLD = 1.0
RECOVERY_CONFIDENCE_TIME_THRESHOLDS_MS = {
    "low": 105_000,
    "medium": 75_000,
    "high": 45_000,
}


def _state_key(student_id: str, course_id: str) -> dict:
    return {"student_id": student_id, "course_id": course_id}


def _normalize_source_text(source_text: str) -> str:
    return " ".join((source_text or "").split())


def _make_source_scope_key(source_text: str) -> str:
    normalized_source = _normalize_source_text(source_text)
    return hashlib.sha256(normalized_source.encode("utf-8")).hexdigest()[:16]


def _make_question_hash(question: dict) -> str:
    options = question.get("options", {}) or {}
    sorted_options = {
        key: options[key]
        for key in sorted(options)
    }
    payload = {
        "question": str(question.get("question", "")).strip(),
        "options": sorted_options,
        "correct_answer": str(question.get("correct_answer", "")).strip(),
        "topic": str(question.get("topic", "")).strip(),
        "difficulty": question.get("difficulty"),
    }
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _prune_recent_question_history(history: list[dict] | None) -> list[dict]:
    if not isinstance(history, list):
        return []

    cutoff = datetime.now(timezone.utc) - RECENT_QUESTION_HISTORY_MAX_AGE
    pruned: list[dict] = []

    for entry in history:
        if not isinstance(entry, dict):
            continue

        question_hash = str(entry.get("question_hash", "")).strip()
        course_id = str(entry.get("course_id", "")).strip()
        seen_at = _parse_iso_datetime(entry.get("seen_at"))

        if not question_hash or not course_id or seen_at is None or seen_at < cutoff:
            continue

        difficulty = entry.get("difficulty")
        try:
            difficulty = int(difficulty)
        except (TypeError, ValueError):
            difficulty = None

        pruned.append({
            "question_hash": question_hash,
            "course_id": course_id,
            "topic": str(entry.get("topic", "")).strip(),
            "difficulty": difficulty,
            "source_scope_key": str(entry.get("source_scope_key", "")).strip(),
            "prompt_version": str(entry.get("prompt_version", "")).strip(),
            "seen_at": seen_at.isoformat(),
        })

    pruned.sort(key=lambda item: item["seen_at"], reverse=True)
    return pruned[:RECENT_QUESTION_HISTORY_LIMIT]


def _recent_seen_hashes_for_course(history: list[dict], course_id: str) -> list[str]:
    hashes: list[str] = []
    seen: set[str] = set()

    for entry in history:
        if entry.get("course_id") != course_id:
            continue

        question_hash = str(entry.get("question_hash", "")).strip()
        if not question_hash or question_hash in seen:
            continue

        seen.add(question_hash)
        hashes.append(question_hash)

    return hashes


def _question_hash_for_tracking(question: dict) -> str:
    question_hash = str(question.get("question_hash", "")).strip()
    if question_hash:
        return question_hash
    return _make_question_hash(question)


def _hash_preview(question_hash: str) -> str:
    return question_hash[:12] if question_hash else "unknown"


def _cache_unused_expires_at(now: datetime) -> datetime:
    return now + timedelta(days=CACHE_UNUSED_TTL_DAYS)


def _cache_used_expires_at(now: datetime) -> datetime:
    return now + timedelta(days=CACHE_USED_TTL_DAYS)


def _prepare_recent_question_history(state: dict) -> list[dict]:
    history = _prune_recent_question_history(state.get("recent_question_history", []))
    state["recent_question_history"] = history
    return history


def _recent_seen_hashes_from_state(state: dict, course_id: str) -> list[str]:
    history = _prepare_recent_question_history(state)
    hashes = _recent_seen_hashes_for_course(history, course_id)
    logger.info("[DEDUPE] Recent hashes course=%s count=%s", course_id, len(hashes))
    return hashes


def _record_seen_question(
    state: dict,
    question: dict,
    course_id: str,
    topic: str,
    difficulty: int,
    source_scope_key: str,
    prompt_version: str,
) -> str:
    question_hash = _question_hash_for_tracking(question)
    history = _prepare_recent_question_history(state)
    now_iso = datetime.now(timezone.utc).isoformat()

    history = [
        entry for entry in history
        if not (
            entry.get("course_id") == course_id
            and entry.get("question_hash") == question_hash
        )
    ]

    history.insert(0, {
        "question_hash": question_hash,
        "course_id": course_id,
        "topic": topic,
        "difficulty": int(difficulty),
        "source_scope_key": source_scope_key,
        "prompt_version": prompt_version,
        "seen_at": now_iso,
    })
    state["recent_question_history"] = _prune_recent_question_history(history)
    logger.info(
        "[DEDUPE] Recorded seen question course=%s topic=%s hash=%s",
        course_id,
        topic,
        _hash_preview(question_hash),
    )
    return question_hash


async def _generate_question_with_soft_dedupe(
    topic: str,
    difficulty: int,
    source_text: str,
    course_id: str,
    recent_hashes: list[str],
) -> dict:
    recent_hash_set = set(recent_hashes)
    attempts = LIVE_DEDUPE_MAX_ATTEMPTS if recent_hash_set else 1

    for attempt in range(1, attempts + 1):
        question = await generate_question(
            topic=topic,
            difficulty=difficulty,
            source_text=source_text,
        )
        question_hash = _question_hash_for_tracking(question)

        if question_hash not in recent_hash_set:
            return question

        logger.info(
            "[DEDUPE] Live repeat detected course=%s topic=%s difficulty=%s attempt=%s hash=%s",
            course_id,
            topic,
            difficulty,
            attempt,
            _hash_preview(question_hash),
        )

        if attempt < attempts:
            logger.info(
                "[DEDUPE] Live repeat retry course=%s topic=%s difficulty=%s attempt=%s",
                course_id,
                topic,
                difficulty,
                attempt + 1,
            )
            continue

        logger.info(
            "[DEDUPE] Live dedupe relaxed course=%s topic=%s difficulty=%s attempts=%s",
            course_id,
            topic,
            difficulty,
            attempts,
        )
        return question

    raise RuntimeError("Soft dedupe generation exhausted unexpectedly.")


def _cache_bucket_key(
    topic: str,
    difficulty: int,
    course_id: str,
    source_scope_key: str,
    prompt_version: str,
) -> str:
    return "|".join([
        course_id,
        topic,
        str(difficulty),
        source_scope_key,
        prompt_version,
    ])


def _normalize_session_origin(session_origin: str | None) -> str:
    normalized = str(session_origin or "").strip().lower()
    if normalized == SESSION_ORIGIN_FOLLOWUP:
        return SESSION_ORIGIN_FOLLOWUP
    return SESSION_ORIGIN_STANDARD


def _is_followup_origin(session_origin: str | None) -> bool:
    return _normalize_session_origin(session_origin) == SESSION_ORIGIN_FOLLOWUP


def _clear_recovery_state(state: dict) -> None:
    state["pending_recovery_offer"] = None
    state["active_recovery_step"] = None


def _normalize_recovery_time_context(value: str | None) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in {"thinking", "distracted", "unknown"}:
        return normalized
    return None


def _is_recovery_supported_session(mode: str | None, session_origin: str | None) -> bool:
    normalized_mode = str(mode or "").strip().lower()
    return (
        _normalize_session_origin(session_origin) == SESSION_ORIGIN_STANDARD
        and normalized_mode in {"normal_practice", "weakness_review"}
    )


def _session_wrong_attempt_count(question_log: list[dict] | None, topic: str) -> int:
    if not question_log:
        return 0
    return sum(
        1
        for entry in question_log
        if (
            entry.get("topic") == topic
            and not entry.get("is_recovery_step")
            and not entry.get("is_correct")
        )
    )


def _session_has_recovery_for_topic(question_log: list[dict] | None, topic: str) -> bool:
    if not question_log:
        return False
    return any(
        (
            entry.get("topic") == topic or entry.get("recovery_for_topic") == topic
        )
        and (
            entry.get("is_recovery_step")
            or entry.get("recovery_step_available")
        )
        for entry in question_log
    )


def _detect_recovery_trigger(
    *,
    current_mode: str | None,
    session_origin: str | None,
    session_complete: bool,
    is_correct: bool,
    topic: str,
    time_spent_ms: int,
    time_context: str | None,
    confidence: str | None,
    question_log: list[dict] | None,
) -> str | None:
    if is_correct or session_complete or not _is_recovery_supported_session(current_mode, session_origin):
        return None

    normalized_mode = str(current_mode or "").strip().lower()
    if normalized_mode == "weakness_review" and _session_has_recovery_for_topic(question_log, topic):
        return None

    if _session_wrong_attempt_count(question_log, topic) >= 1:
        return RECOVERY_REASON_REPEATED

    normalized_time_context = _normalize_recovery_time_context(time_context)
    if normalized_time_context == "distracted":
        return None

    if normalized_time_context == "thinking":
        return RECOVERY_REASON_THOUGHTFUL

    normalized_confidence = normalize_confidence_level(confidence)
    thoughtful_threshold = RECOVERY_CONFIDENCE_TIME_THRESHOLDS_MS.get(
        normalized_confidence,
        RECOVERY_THOUGHTFUL_RESPONSE_MS,
    )
    if time_spent_ms >= thoughtful_threshold:
        return RECOVERY_REASON_THOUGHTFUL

    return None


def _set_pending_recovery_offer(
    state: dict,
    *,
    session_id: str,
    topic: str,
    difficulty: int,
    trigger_reason: str,
    question_id: str | None,
) -> None:
    state["pending_recovery_offer"] = {
        "session_id": session_id,
        "topic": topic,
        "difficulty": int(difficulty),
        "trigger_reason": trigger_reason,
        "question_id": str(question_id or "").strip(),
        "status": "offered",
        "offered_at": datetime.now(timezone.utc).isoformat(),
    }
    state["active_recovery_step"] = None


def _activate_recovery_step(
    state: dict,
    *,
    session_id: str,
    topic: str,
    trigger_reason: str,
    recovery_difficulty: int,
    recovery_question_id: str,
) -> None:
    state["pending_recovery_offer"] = None
    state["active_recovery_step"] = {
        "session_id": session_id,
        "topic": topic,
        "trigger_reason": trigger_reason,
        "recovery_difficulty": int(recovery_difficulty),
        "recovery_question_id": recovery_question_id,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
    }


def _recovery_reason_label(reason: str | None) -> str | None:
    if reason == RECOVERY_REASON_REPEATED:
        return "repeated same-topic errors"
    if reason == RECOVERY_REASON_THOUGHTFUL:
        return "long thoughtful struggle"
    return None


def _is_focused_followup_session(
    mode: str,
    focus_topics: list[str] | None,
    session_origin: str | None,
) -> bool:
    normalized_focus_topics = _dedupe_keep_order([
        str(topic).strip()
        for topic in (focus_topics or [])
        if str(topic).strip()
    ])
    return (
        mode == "weakness_review"
        and _is_followup_origin(session_origin)
        and len(normalized_focus_topics) == 1
    )


def _state_is_focused_followup(state: dict) -> bool:
    return bool(state.get("current_is_focused_followup"))


def _cache_target_for_state(state: dict, topic: str, difficulty: int) -> int:
    if _state_is_focused_followup(state):
        logger.info(
            "[FOLLOWUP] Focused hot bucket target=%s topic=%s difficulty=%s",
            FOCUSED_FOLLOWUP_CACHE_TARGET,
            topic,
            difficulty,
        )
        return FOCUSED_FOLLOWUP_CACHE_TARGET

    return DEFAULT_CACHE_TARGET


async def _wait_for_replenished_cache(
    topic: str,
    difficulty: int,
    course_id: str,
    source_scope_key: str,
    prompt_version: str,
) -> bool:
    bucket_key = _cache_bucket_key(
        topic=topic,
        difficulty=difficulty,
        course_id=course_id,
        source_scope_key=source_scope_key,
        prompt_version=prompt_version,
    )
    if bucket_key not in _REPLENISH_IN_FLIGHT:
        return False

    logger.info(
        "[FOLLOWUP] Waiting for inflight replenish topic=%s difficulty=%s",
        topic,
        difficulty,
    )

    attempts = max(
        1,
        int(FOCUSED_FOLLOWUP_WAIT_SECONDS / FOCUSED_FOLLOWUP_WAIT_INTERVAL_SECONDS),
    )
    for _ in range(attempts):
        await asyncio.sleep(FOCUSED_FOLLOWUP_WAIT_INTERVAL_SECONDS)
        if bucket_key not in _REPLENISH_IN_FLIGHT:
            break

    return True

def _mastery_label(mastery: float) -> str:
    if mastery < 0.30:
        return "Struggling"
    if mastery < 0.50:
        return "Emerging"
    if mastery < 0.65:
        return "Developing"
    if mastery < 0.80:
        return "Proficient"
    return "Mastered"


def _build_challenge_readiness(topic_mastery: dict, scoped_topics: list[str]) -> dict:
    normalized_topics = _dedupe_keep_order([
        str(topic).strip()
        for topic in (scoped_topics or [])
        if str(topic).strip()
    ])
    if not normalized_topics:
        return {
            "ready": False,
            "scoped_topic_count": 0,
            "avg_mastery": 0.0,
            "proficient_topic_count": 0,
            "required_proficient_topics": 0,
        }

    scoped_scores = [
        float(topic_mastery.get(topic, 0.5))
        for topic in normalized_topics
    ]
    avg_mastery = sum(scoped_scores) / len(scoped_scores)
    proficient_topic_count = sum(
        1 for score in scoped_scores
        if _mastery_label(score) in {"Proficient", "Mastered"}
    )
    required_proficient_topics = 1 if len(normalized_topics) == 1 else (len(normalized_topics) + 1) // 2

    if len(normalized_topics) == 1:
        ready = scoped_scores[0] >= CHALLENGE_READY_AVG_MASTERY
    else:
        ready = (
            avg_mastery >= CHALLENGE_READY_AVG_MASTERY
            and proficient_topic_count >= required_proficient_topics
        )

    return {
        "ready": ready,
        "scoped_topic_count": len(normalized_topics),
        "avg_mastery": round(avg_mastery, 4),
        "proficient_topic_count": proficient_topic_count,
        "required_proficient_topics": required_proficient_topics,
    }


def _challenge_not_ready_response(readiness: dict) -> dict:
    return {
        "success": False,
        "error": CHALLENGE_NOT_READY_ERROR,
        "message": "Challenge mode is not available for this lecture yet.",
        "challenge_readiness": readiness,
    }

def _difficulty_label(diff: int) -> str:
    return {
        1: "very easy",
        2: "easy",
        3: "medium",
        4: "hard",
        5: "very hard",
    }.get(diff, "medium")


def _build_narrative_bridge(
    is_correct: bool,
    current_topic: str,
    next_topic: str,
    next_mode: str,
    session_origin: str | None,
    current_difficulty: int,
    next_difficulty: int,
    consecutive_wrong: int,
    current_topic_mastery: float,
    next_topic_mastery: float,
) -> str:
    current_diff_label = _difficulty_label(current_difficulty)
    next_diff_label = _difficulty_label(next_difficulty)

    mode = (next_mode or "normal_practice").lower()

    def _topic_relation() -> str:
        if next_topic == current_topic:
            return "the same topic"
        return next_topic

    if _is_followup_origin(session_origin):
        if is_correct:
            if next_topic == current_topic:
                if next_difficulty > current_difficulty:
                    return (
                        f"You are improving on {current_topic}, so this follow-up stays focused there "
                        f"and raises the challenge from {current_diff_label} to {next_diff_label}."
                    )
                return (
                    f"You answered correctly, and this follow-up keeps reinforcing {current_topic} "
                    f"so you can stabilise it through focused practice."
                )
            return (
                f"You answered correctly, and this follow-up now continues on {next_topic} "
                f"to keep reinforcing the same weak area from a slightly different angle."
            )

        if consecutive_wrong >= 2:
            return (
                f"This follow-up keeps the focus on {current_topic} because repeated errors suggest "
                f"the concept still needs steady, step-by-step reinforcement."
            )

        if next_topic != current_topic:
            return (
                f"This follow-up now shifts to {next_topic} to keep reinforcing the same area of weakness."
            )

        if next_difficulty < current_difficulty:
            return (
                f"This follow-up stays on {current_topic} and lowers the difficulty from "
                f"{current_diff_label} to {next_diff_label} so you can rebuild confidence."
            )

        return (
            f"This follow-up keeps the focus on {current_topic} so you can strengthen it before moving on."
        )

    if mode == "weakness_review":
        if is_correct:
            if next_topic == current_topic:
                if next_difficulty > current_difficulty:
                    return (
                        f"You are improving on {current_topic}, so weakness review keeps you there "
                        f"and raises the challenge from {current_diff_label} to {next_diff_label}."
                    )
                return (
                    f"You answered correctly, and weakness review keeps the focus on {current_topic} "
                    f"to stabilise it through guided repetition."
                )
            return (
                f"You answered correctly, and weakness review now rotates to {next_topic} "
                f"because it is another area that still needs reinforcement."
            )

        if consecutive_wrong >= 2:
            return (
                f"Weakness review is staying close to {current_topic} because repeated errors suggest "
                f"this concept still needs step-by-step support."
            )

        if next_topic != current_topic:
            return (
                f"This answer suggests {next_topic} also needs reinforcement, so weakness review rotates there next."
            )

        if next_difficulty < current_difficulty:
            return (
                f"Weakness review keeps you on {current_topic} and lowers the difficulty from "
                f"{current_diff_label} to {next_diff_label} to rebuild confidence."
            )

        return (
            f"Weakness review keeps the focus on {current_topic} so you can strengthen it before moving on."
        )

    if mode == "challenge":
        if is_correct:
            if next_topic != current_topic:
                return (
                    f"You handled {current_topic} well, so challenge mode now pushes you toward {next_topic}, "
                    f"one of your stronger areas, at a {next_diff_label} level."
                )
            if next_difficulty > current_difficulty:
                return (
                    f"You answered correctly, so challenge mode keeps you on {current_topic} "
                    f"and increases the difficulty from {current_diff_label} to {next_diff_label}."
                )
            return (
                f"You answered correctly, so challenge mode continues on {current_topic} "
                f"to stretch your understanding at a {next_diff_label} level."
            )

        if next_topic != current_topic:
            return (
                f"Even though that one was difficult, challenge mode keeps the session ambitious "
                f"by moving to {_topic_relation()}."
            )

        if next_difficulty < current_difficulty:
            return (
                f"Challenge mode is still pushing your stronger path, but it eases from "
                f"{current_diff_label} to {next_diff_label} so the stretch stays productive."
            )

        return (
            f"Challenge mode keeps the pressure on {current_topic} so you continue working at the edge of mastery."
        )

    # normal_practice / auto-resolved normal
    if is_correct:
        if next_topic != current_topic:
            if next_topic_mastery > 0.70:
                return (
                    f"You handled {current_topic} well, so normal practice briefly shifts to {next_topic} "
                    f"for spaced review and retention at a {next_diff_label} level."
                )
            if next_topic_mastery < 0.45:
                return (
                    f"You handled {current_topic} well, and normal practice now checks in on {next_topic} "
                    f"because it still looks weaker."
                )
            return (
                f"You handled {current_topic} well, so normal practice moves to {next_topic} "
                f"because it is a good next step for growth."
            )

        if next_difficulty > current_difficulty:
            return (
                f"You answered correctly, so normal practice keeps the focus on {current_topic} "
                f"and increases the challenge from {current_diff_label} to {next_diff_label}."
            )

        return (
            f"You answered correctly, so normal practice stays on {current_topic} "
            f"to consolidate it at a {next_diff_label} level."
        )

    if consecutive_wrong >= 2:
        return (
            f"Normal practice is slowing down around {current_topic} because repeated errors suggest "
            f"this concept needs more guided reinforcement before moving on."
        )

    if next_topic != current_topic:
        if next_topic_mastery > 0.70:
            return (
                f"Normal practice now shifts to {next_topic} as a spaced-review checkpoint, "
                f"so the session stays balanced instead of drilling one area too long."
            )
        if next_topic_mastery < 0.45:
            return (
                f"This answer suggests {next_topic} needs attention, so normal practice redirects there next."
            )
        return (
            f"Normal practice now moves to {next_topic} because it is the most useful next topic for progress."
        )

    if next_difficulty < current_difficulty:
        return (
            f"This concept still needs support, so normal practice stays on {current_topic} "
            f"and lowers the difficulty from {current_diff_label} to {next_diff_label}."
        )

    return (
        f"Normal practice keeps the focus on {current_topic} so you can strengthen it before the session moves on."
    )

async def _compute_overall_stats(student_id: str, course_id: str) -> dict:
    db = get_db()

    pipeline = [
        {
            "$match": {
                "student_id": student_id,
                "course_id": course_id,
                "ended_at": {"$ne": None},
            }
        },
        {
            "$group": {
                "_id": None,
                "completed_sessions": {"$sum": 1},
                "completed_questions_answered": {"$sum": "$questions_answered"},
                "completed_correct_answers": {"$sum": "$correct_answers"},
                "completed_total_time_spent_ms": {"$sum": "$total_time_spent_ms"},
            }
        }
    ]

    stats = await db.student_session_history.aggregate(pipeline).to_list(1)
    if not stats:
        return {
            "completed_sessions": 0,
            "completed_questions_answered": 0,
            "completed_correct_answers": 0,
            "overall_accuracy": None,
            "overall_avg_time_spent_ms": None,
        }

    row = stats[0]
    total_answered = row.get("completed_questions_answered", 0)
    total_correct = row.get("completed_correct_answers", 0)
    total_time = row.get("completed_total_time_spent_ms", 0)

    overall_accuracy = None
    overall_avg_time_spent_ms = None

    if total_answered > 0:
        overall_accuracy = round(total_correct / total_answered, 4)
        overall_avg_time_spent_ms = int(total_time / total_answered)

    return {
        "completed_sessions": row.get("completed_sessions", 0),
        "completed_questions_answered": total_answered,
        "completed_correct_answers": total_correct,
        "overall_accuracy": overall_accuracy,
        "overall_avg_time_spent_ms": overall_avg_time_spent_ms,
    }

async def _get_state(student_id: str, course_id: str, topics: list[str]) -> dict:
    db = get_db()
    state = await db.student_states.find_one(_state_key(student_id, course_id))
    if not state:
        state = get_initial_student_state(student_id, course_id, topics)
        await db.student_states.insert_one(state)
    return state


async def _save_state(state: dict) -> None:
    db = get_db()
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    await db.student_states.update_one(
        _state_key(state["student_id"], state["course_id"]),
        {"$set": state},
        upsert=True
    )

async def _create_session_history(
    student_id: str,
    course_id: str,
    session_topics: list[str],
    mastery_before: dict,
    start_difficulty: int,
    target_questions: int,
    selected_content_ids: list[str],
    selected_content_titles: list[str],
    selected_mode: str,
    session_origin: str,
) -> str:
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    session_id = str(uuid4())

    doc = {
        "session_id": session_id,
        "student_id": student_id,
        "course_id": course_id,
        "started_at": now,
        "ended_at": None,
        "target_questions": target_questions,
        "questions_answered": 0,
        "correct_answers": 0,
        "accuracy": 0.0,
        "selected_content_ids": selected_content_ids,
        "selected_content_titles": selected_content_titles,
        "session_topics": session_topics,
        "start_difficulty": start_difficulty,
        "end_difficulty": start_difficulty,
        "difficulty_path": [],
        "topic_mastery_before": mastery_before,
        "topic_mastery_after": {},
        "question_log": [],
        "avg_time_spent_ms": 0,
        "total_time_spent_ms": 0,
        "weakest_topic_this_session": None,
        "strongest_topic_this_session": None,
        "recommended_review_topic": None,
        "recommended_review_topics": [],
        "review_pressure_by_topic": {},
        "followup_topics_practised": [],
        "followup_topic_mastery_summaries": [],
        "recommendation": None,
        "selected_mode": selected_mode,
        "session_origin": session_origin,
    }

    await db.student_session_history.insert_one(doc)
    return session_id


async def _append_question_log(
    session_id: str,
    question_entry: dict,
    is_correct: bool,
    time_spent_ms: int,
    question_difficulty: int,
    end_difficulty: int,
    counts_toward_session: bool = True,
):
    db = get_db()
    update_doc: dict = {
        "$push": {
            "question_log": question_entry,
        },
        "$set": {
            "end_difficulty": end_difficulty,
        }
    }

    if counts_toward_session:
        update_doc["$push"]["difficulty_path"] = question_difficulty
        update_doc["$inc"] = {
            "questions_answered": 1,
            "correct_answers": 1 if is_correct else 0,
            "total_time_spent_ms": time_spent_ms,
        }

    await db.student_session_history.update_one(
        {"session_id": session_id},
        update_doc,
    )
    
def _compute_topic_session_stats(question_log: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}

    for entry in question_log:
        if entry.get("is_recovery_step") or entry.get("counts_toward_session_score") is False:
            continue

        topic = entry.get("topic")
        if not topic:
            continue

        if topic not in stats:
            stats[topic] = {
                "attempts": 0,
                "correct": 0,
                "wrong": 0,
                "total_time_spent_ms": 0,
                "accuracy": 0.0,
                "avg_time_spent_ms": 0,
            }

        stats[topic]["attempts"] += 1
        stats[topic]["total_time_spent_ms"] += int(entry.get("time_spent_ms", 0) or 0)

        if entry.get("is_correct"):
            stats[topic]["correct"] += 1
        else:
            stats[topic]["wrong"] += 1

    for topic, s in stats.items():
        attempts = s["attempts"]
        s["accuracy"] = round(s["correct"] / attempts, 4) if attempts > 0 else 0.0
        s["avg_time_spent_ms"] = int(s["total_time_spent_ms"] / attempts) if attempts > 0 else 0

    return stats


def _compute_topic_review_pressure(question_log: list[dict]) -> dict[str, dict]:
    pressure_by_topic: dict[str, dict] = {}

    for entry in question_log:
        topic = (
            str(entry.get("recovery_for_topic") or "").strip()
            if entry.get("is_recovery_step")
            else str(entry.get("topic") or "").strip()
        )
        if not topic:
            continue

        topic_pressure = pressure_by_topic.setdefault(topic, {
            "pressure": 0.0,
            "recovery_attempted": False,
            "recovery_successes": 0,
            "recovery_failures": 0,
            "weak_events": 0,
        })

        if entry.get("is_recovery_step"):
            topic_pressure["recovery_attempted"] = True
            if entry.get("is_correct"):
                topic_pressure["recovery_successes"] += 1
                topic_pressure["pressure"] = max(0.0, topic_pressure["pressure"] - 1.0)
            else:
                topic_pressure["recovery_failures"] += 1
                topic_pressure["pressure"] += 1.0
            continue

        if entry.get("is_correct"):
            continue

        base_pressure = 1.0
        if _normalize_recovery_time_context(entry.get("time_context")) == "thinking":
            base_pressure += 0.25
        if entry.get("recovery_trigger_reason") == RECOVERY_REASON_REPEATED:
            base_pressure += 0.5
        elif entry.get("recovery_trigger_reason") == RECOVERY_REASON_THOUGHTFUL:
            base_pressure += 0.25

        topic_pressure["pressure"] += base_pressure
        topic_pressure["weak_events"] += 1

    for topic_data in pressure_by_topic.values():
        topic_data["pressure"] = round(max(0.0, topic_data["pressure"]), 4)

    return pressure_by_topic


def _select_recommended_review_topics(
    topic_session_stats: dict[str, dict],
    review_pressure_by_topic: dict[str, dict] | None = None,
    max_topics: int = 2,
) -> list[str]:
    review_pressure_by_topic = review_pressure_by_topic or {}
    all_topics = set(topic_session_stats) | set(review_pressure_by_topic)
    if not all_topics:
        return []

    review_candidates = []
    for topic in all_topics:
        stats = topic_session_stats.get(topic, {})
        pressure = (review_pressure_by_topic.get(topic) or {}).get("pressure", 0.0)
        recovery_failures = (review_pressure_by_topic.get(topic) or {}).get("recovery_failures", 0)
        multiple_weak_signals = (
            stats.get("wrong", 0) >= 2
            or (stats.get("attempts", 0) >= 2 and stats.get("accuracy", 1.0) < 0.6)
            or recovery_failures >= 1
        )
        isolated_weak_signal = stats.get("wrong", 0) >= 1 or pressure >= RECOVERY_REVIEW_PRESSURE_THRESHOLD

        if (
            pressure >= 1.5
            or (pressure >= RECOVERY_REVIEW_PRESSURE_THRESHOLD and isolated_weak_signal)
            or (multiple_weak_signals and pressure >= 0.75)
        ):
            review_candidates.append(topic)

    ranked = sorted(
        review_candidates,
        key=lambda topic: (
            -float((review_pressure_by_topic.get(topic) or {}).get("pressure", 0.0)),
            topic_session_stats.get(topic, {}).get("accuracy", 1.0),
            -topic_session_stats.get(topic, {}).get("wrong", 0),
            -topic_session_stats.get(topic, {}).get("attempts", 0),
            -topic_session_stats.get(topic, {}).get("avg_time_spent_ms", 0),
            topic,
        ),
    )
    return ranked[:max(0, max_topics)]


def _get_practiced_topics_from_doc(doc: dict) -> list[str]:
    practiced_topics = _dedupe_keep_order([
        str(entry.get("topic", "")).strip()
        for entry in (doc.get("question_log") or [])
        if str(entry.get("topic", "")).strip()
    ])
    if practiced_topics:
        return practiced_topics

    return _dedupe_keep_order([
        str(topic).strip()
        for topic in (doc.get("practiced_topics") or [])
        if str(topic).strip()
    ])


def _build_followup_topic_mastery_summaries(doc: dict, topic_mastery_after: dict) -> list[dict]:
    if not _is_followup_origin(doc.get("session_origin")):
        return []

    topic_mastery_before = doc.get("topic_mastery_before", {}) or {}
    summaries: list[dict] = []

    for topic in _get_practiced_topics_from_doc(doc)[:2]:
        if topic not in topic_mastery_before or topic not in topic_mastery_after:
            continue

        before = float(topic_mastery_before[topic])
        after = float(topic_mastery_after[topic])
        summaries.append({
            "topic": topic,
            "mastery_before": round(before, 4),
            "mastery_after": round(after, 4),
            "mastery_delta": round(after - before, 4),
        })

    return summaries

def _join_topics(topics: list[str]) -> str:
    if not topics:
        return ""
    if len(topics) == 1:
        return topics[0]
    if len(topics) == 2:
        return f"{topics[0]} and {topics[1]}"
    return ", ".join(topics[:-1]) + f", and {topics[-1]}"


def _make_recommendation_payload(code: str, title: str, text: str) -> dict[str, str]:
    return {
        "code": code,
        "title": title,
        "text": text,
        "legacy_text": text,
    }


async def _get_selected_scope_topics(doc: dict) -> list[str]:
    db = get_db()
    selected_content_ids = doc.get("selected_content_ids", []) or []
    course_id = doc.get("course_id")

    if selected_content_ids and course_id:
        object_ids = []
        for content_id in selected_content_ids:
            try:
                object_ids.append(ObjectId(content_id))
            except Exception:
                continue

        if object_ids:
            content_docs = await db.course_content.find({
                "_id": {"$in": object_ids},
                "course_id": course_id,
            }).to_list(length=len(object_ids))

            scoped_topics: list[str] = []
            for item in content_docs:
                scoped_topics.extend(item.get("topics", []) or [])

            deduped_topics = _dedupe_keep_order([
                str(topic).strip()
                for topic in scoped_topics
                if str(topic).strip()
            ])
            if deduped_topics:
                return deduped_topics

    fallback_topics = doc.get("session_topics") or doc.get("practiced_topics") or []
    return _dedupe_keep_order([
        str(topic).strip()
        for topic in fallback_topics
        if str(topic).strip()
    ])


def _build_session_recommendation(
    *,
    accuracy: float,
    review_topics: list[str] | None,
    strongest_topic: str | None,
    selected_mode: str | None,
    session_origin: str | None,
    followup_topics: list[str] | None = None,
    scoped_topics: list[str] | None = None,
    topic_session_stats: dict[str, dict] | None = None,
    review_pressure_by_topic: dict[str, dict] | None = None,
    challenge_ready: bool = False,
    challenge_scope_topic_count: int = 0,
) -> dict[str, str] | None:
    normalized_review_topics = _dedupe_keep_order([
        str(topic).strip()
        for topic in (review_topics or [])
        if str(topic).strip()
    ])
    followup_topics = _dedupe_keep_order([
        str(topic).strip()
        for topic in (followup_topics or [])
        if str(topic).strip()
    ])
    scoped_topics = _dedupe_keep_order([
        str(topic).strip()
        for topic in (scoped_topics or [])
        if str(topic).strip()
    ])
    topic_session_stats = topic_session_stats or {}
    review_pressure_by_topic = review_pressure_by_topic or {}

    normalized_mode = str(selected_mode or "").strip().lower() or "normal_practice"
    is_followup = _is_followup_origin(session_origin)
    is_challenge = normalized_mode == "challenge"
    is_weakness_review = normalized_mode == "weakness_review"

    practiced_topic_count = max(len(topic_session_stats), len(scoped_topics))
    poor_topics: list[str] = []
    recovery_failures_total = 0

    for topic, stats in topic_session_stats.items():
        pressure = review_pressure_by_topic.get(topic, {}) or {}
        recovery_failures = int(pressure.get("recovery_failures", 0) or 0)
        recovery_failures_total += recovery_failures
        if (
            stats.get("wrong", 0) >= 2
            or stats.get("accuracy", 1.0) < 0.6
            or float(pressure.get("pressure", 0.0) or 0.0) >= 1.5
            or recovery_failures >= 1
        ):
            poor_topics.append(topic)

    focus_topics = (followup_topics or normalized_review_topics)[:2] if is_followup else normalized_review_topics[:2]
    focus_topic_text = _join_topics(focus_topics)
    primary_focus_topic = focus_topics[0] if focus_topics else None
    primary_focus_stats = topic_session_stats.get(primary_focus_topic or "", {})
    primary_focus_pressure = review_pressure_by_topic.get(primary_focus_topic or "", {}) or {}
    primary_focus_failures = int(primary_focus_pressure.get("recovery_failures", 0) or 0)
    primary_focus_successes = int(primary_focus_pressure.get("recovery_successes", 0) or 0)
    primary_focus_pressure_value = float(primary_focus_pressure.get("pressure", 0.0) or 0.0)

    broad_topic_threshold = 2 if practiced_topic_count <= 2 else (practiced_topic_count + 1) // 2
    broad_weakness = (
        len(poor_topics) >= 2
        and (
            accuracy < 0.6
            or len(poor_topics) >= broad_topic_threshold
            or len(normalized_review_topics) >= broad_topic_threshold
            or recovery_failures_total >= 2
        )
    )
    isolated_recovery_stabilized = (
        len(focus_topics) == 1
        and primary_focus_successes >= 1
        and primary_focus_failures == 0
        and accuracy >= 0.6
        and primary_focus_pressure_value < 1.5
    )
    focused_followup_justified = bool(focus_topics) and not broad_weakness and not isolated_recovery_stabilized
    strong_session = accuracy >= 0.8
    no_repair_signal = not normalized_review_topics and not poor_topics
    challenge_recommendable = challenge_ready and strong_session and no_repair_signal and not is_challenge

    if is_challenge:
        if broad_weakness or accuracy < 0.55:
            topic_text = _join_topics(normalized_review_topics[:2] or poor_topics[:2])
            return _make_recommendation_payload(
                "revisit_lecture_content",
                "Recommended next step: Revisit this lecture first",
                (
                    f"This challenge exposed gaps across {topic_text}, so revisiting the lecture before another quiz is the best next step."
                ) if topic_text else
                "This challenge exposed broader gaps in this lecture, so revisiting the lecture before another quiz is the best next step."
            )
        if focused_followup_justified and focus_topic_text:
            return _make_recommendation_payload(
                "focused_follow_up",
                "Recommended next step: Do a focused follow-up",
                f"Challenge mode exposed the most difficulty in {focus_topic_text}, so a short targeted follow-up there is the best next step."
            )
        return _make_recommendation_payload(
            "continue_normal_practice",
            "Recommended next step: Continue normal practice",
            "You handled this challenge well, so returning to normal practice is the best next step to keep building breadth."
        )

    if is_followup:
        if broad_weakness or accuracy < 0.55:
            if (
                focused_followup_justified
                and len(focus_topics) == 1
                and (primary_focus_pressure_value >= 1.5 or primary_focus_failures >= 1 or primary_focus_stats.get("wrong", 0) >= 2)
            ):
                return _make_recommendation_payload(
                    "focused_follow_up",
                    "Recommended next step: Do a focused follow-up",
                    f"{focus_topic_text} still needs concentrated work, so one more short focused follow-up there is the best next step."
                )
            topic_text = _join_topics(normalized_review_topics[:2] or poor_topics[:2])
            return _make_recommendation_payload(
                "revisit_lecture_content",
                "Recommended next step: Revisit this lecture first",
                (
                    f"This follow-up still showed gaps across {topic_text}, so rereading the lecture before another quiz will help more than repeating the same practice loop."
                ) if topic_text else
                "This follow-up still showed broader gaps, so rereading the lecture before another quiz will help more than repeating the same practice loop."
            )
        if challenge_recommendable:
            return _make_recommendation_payload(
                "try_challenge_mode",
                "Recommended next step: Try challenge mode",
                f"This follow-up was strong, and challenge mode is unlocked for this lecture scope, so a more demanding session is the best next step."
            )
        if focused_followup_justified and len(focus_topics) == 1 and accuracy < 0.75 and (
            primary_focus_pressure_value >= 1.5 or primary_focus_failures >= 1 or primary_focus_stats.get("wrong", 0) >= 2
        ):
            return _make_recommendation_payload(
                "focused_follow_up",
                "Recommended next step: Do a focused follow-up",
                f"{focus_topic_text} still needs a little more reinforcement, so a short focused follow-up there is the best next step."
            )
        return _make_recommendation_payload(
            "continue_normal_practice",
            "Recommended next step: Continue normal practice",
            "This follow-up showed steadier performance, so returning to normal practice is the best next step."
        )

    if is_weakness_review:
        if broad_weakness:
            topic_text = _join_topics(normalized_review_topics[:2] or poor_topics[:2])
            return _make_recommendation_payload(
                "revisit_lecture_content",
                "Recommended next step: Revisit this lecture first",
                (
                    f"Your mistakes were spread across {topic_text}, so revisiting the lecture before another quiz will help more than pushing further right now."
                ) if topic_text else
                "Your mistakes were spread across several topics, so revisiting the lecture before another quiz will help more than pushing further right now."
            )
        if focused_followup_justified and focus_topic_text:
            return _make_recommendation_payload(
                "focused_follow_up",
                "Recommended next step: Do a focused follow-up",
                f"{focus_topic_text} still needs concentrated reinforcement, so a short focused follow-up there is the best next step."
            )
        return _make_recommendation_payload(
            "continue_normal_practice",
            "Recommended next step: Continue normal practice",
            "You stabilized this weak area without one dominant repair signal, so returning to normal practice is the best next step."
        )

    if broad_weakness:
        topic_text = _join_topics(normalized_review_topics[:2] or poor_topics[:2])
        return _make_recommendation_payload(
            "revisit_lecture_content",
            "Recommended next step: Revisit this lecture first",
            (
                f"Your mistakes were spread across {topic_text}, so rereading the lecture before another quiz will likely help more than jumping straight into more questions."
            ) if topic_text else
            "Your mistakes were spread across several topics, so rereading the lecture before another quiz will likely help more than jumping straight into more questions."
        )

    if focused_followup_justified and focus_topic_text:
        return _make_recommendation_payload(
            "focused_follow_up",
            "Recommended next step: Do a focused follow-up",
            (
                f"You struggled most with {focus_topic_text}, so a short targeted follow-up there will help more than switching topics now."
            ) if len(focus_topics) == 1 else
            f"You struggled most with {focus_topic_text}, so a short targeted follow-up on those topics is the best next step."
        )

    if challenge_recommendable:
        return _make_recommendation_payload(
            "try_challenge_mode",
            "Recommended next step: Try challenge mode",
            "Your performance in this lecture is strong and stable enough to try a more demanding practice path."
        )

    if strong_session and strongest_topic:
        return _make_recommendation_payload(
            "continue_normal_practice",
            "Recommended next step: Continue normal practice",
            f"You are improving without one dominant weak topic, so another normal session is the best way to keep building momentum from {strongest_topic}."
        )

    return _make_recommendation_payload(
        "continue_normal_practice",
        "Recommended next step: Continue normal practice",
        "You are improving without one dominant weak topic, so another normal session is the best way to keep building momentum."
    )


async def _build_content_mastery_summaries(doc: dict, topic_mastery_after: dict) -> list[dict]:
    db = get_db()

    selected_content_ids = doc.get("selected_content_ids", [])
    selected_content_titles = doc.get("selected_content_titles", [])
    course_id = doc.get("course_id")
    topic_mastery_before = doc.get("topic_mastery_before", {}) or {}

    if not selected_content_ids or not course_id:
        return []

    title_by_id = {}
    for idx, content_id in enumerate(selected_content_ids):
        if idx < len(selected_content_titles):
            title_by_id[content_id] = selected_content_titles[idx]

    object_ids = []
    for content_id in selected_content_ids:
        try:
            object_ids.append(ObjectId(content_id))
        except Exception:
            continue

    docs_by_id = {}
    if object_ids:
        content_docs = await db.course_content.find({
            "_id": {"$in": object_ids},
            "course_id": course_id,
        }).to_list(length=len(object_ids))
        docs_by_id = {str(item["_id"]): item for item in content_docs}

    summaries = []
    for content_id in selected_content_ids:
        content_doc = docs_by_id.get(content_id)
        topics = content_doc.get("topics", []) if content_doc else []
        matched_topics = [
            topic for topic in topics
            if topic in topic_mastery_before and topic in topic_mastery_after
        ]

        if not matched_topics:
            continue

        avg_before = sum(topic_mastery_before[topic] for topic in matched_topics) / len(matched_topics)
        avg_after = sum(topic_mastery_after[topic] for topic in matched_topics) / len(matched_topics)

        summaries.append({
            "content_id": content_id,
            "title": (
                (content_doc or {}).get("title")
                or title_by_id.get(content_id)
                or "Selected Lecture"
            ),
            "avg_mastery_before": round(avg_before, 4),
            "avg_mastery_after": round(avg_after, 4),
            "mastery_delta": round(avg_after - avg_before, 4),
            "topic_count": len(matched_topics),
        })

    return summaries


def _build_focused_topic_mastery_summary(doc: dict, topic_mastery_after: dict) -> dict | None:
    summaries = _build_followup_topic_mastery_summaries(doc, topic_mastery_after)
    if len(summaries) != 1:
        return None
    return summaries[0]

async def _finalize_session_history(session_id: str, topic_mastery_after: dict) -> dict:
    db = get_db()
    doc = await db.student_session_history.find_one({"session_id": session_id})
    if not doc:
        return {}

    questions_answered = doc.get("questions_answered", 0)
    correct_answers = doc.get("correct_answers", 0)
    total_time_spent_ms = doc.get("total_time_spent_ms", 0)

    accuracy = (correct_answers / questions_answered) if questions_answered > 0 else 0.0
    avg_time = int(total_time_spent_ms / questions_answered) if questions_answered > 0 else 0

    question_log = doc.get("question_log", [])
    practiced_topics = _get_practiced_topics_from_doc(doc)

    lectures_practised_count = len(doc.get("selected_content_ids", []) or [])
    topics_practised_count = len(practiced_topics)
    content_mastery_summaries = await _build_content_mastery_summaries(doc, topic_mastery_after)
    followup_topic_mastery_summaries = _build_followup_topic_mastery_summaries(doc, topic_mastery_after)
    focused_topic_mastery_summary = _build_focused_topic_mastery_summary(doc, topic_mastery_after)
    followup_topics_practised = [summary["topic"] for summary in followup_topic_mastery_summaries]
    scoped_topics = await _get_selected_scope_topics(doc)
    challenge_readiness = _build_challenge_readiness(topic_mastery_after, scoped_topics)

    topic_session_stats = _compute_topic_session_stats(question_log)
    review_pressure_by_topic = _compute_topic_review_pressure(question_log)

    weakest_topic = None
    strongest_topic = None
    recommended_review_topics: list[str] = []

    if topic_session_stats:
        # strongest = best session-local performance, preferring more evidence
        strongest_topic = max(
            topic_session_stats,
            key=lambda t: (
                topic_session_stats[t]["accuracy"],
                topic_session_stats[t]["attempts"],
                -topic_session_stats[t]["avg_time_spent_ms"],
            )
        )

        recommended_review_topics = _select_recommended_review_topics(
            topic_session_stats,
            review_pressure_by_topic=review_pressure_by_topic,
        )
        if recommended_review_topics:
            weakest_topic = recommended_review_topics[0]

    recommended_review_topic = weakest_topic

    recommendation_payload = _build_session_recommendation(
        accuracy=accuracy,
        review_topics=recommended_review_topics,
        strongest_topic=strongest_topic,
        selected_mode=doc.get("selected_mode"),
        session_origin=doc.get("session_origin"),
        followup_topics=followup_topics_practised,
        scoped_topics=scoped_topics,
        topic_session_stats=topic_session_stats,
        review_pressure_by_topic=review_pressure_by_topic,
        challenge_ready=bool(challenge_readiness.get("ready")),
        challenge_scope_topic_count=int(challenge_readiness.get("scoped_topic_count", 0) or 0),
    )
    recommendation = (recommendation_payload or {}).get("legacy_text")

    ended_at = datetime.now(timezone.utc).isoformat()

    await db.student_session_history.update_one(
        {"session_id": session_id},
        {
            "$set": {
                "ended_at": ended_at,
                "accuracy": accuracy,
                "avg_time_spent_ms": avg_time,
                "topic_mastery_after": topic_mastery_after,
                "practiced_topics": practiced_topics,
                "lectures_practised_count": lectures_practised_count,
                "topics_practised_count": topics_practised_count,
                "content_mastery_summaries": content_mastery_summaries,
                "focused_topic_mastery_summary": focused_topic_mastery_summary,
                "followup_topics_practised": followup_topics_practised,
                "followup_topic_mastery_summaries": followup_topic_mastery_summaries,
                "weakest_topic_this_session": weakest_topic,
                "strongest_topic_this_session": strongest_topic,
                "recommended_review_topic": recommended_review_topic,
                "recommended_review_topics": recommended_review_topics,
                "recommendation": recommendation,
                "recommendation_code": (recommendation_payload or {}).get("code"),
                "recommendation_title": (recommendation_payload or {}).get("title"),
                "recommendation_text": (recommendation_payload or {}).get("text"),
                "review_pressure_by_topic": review_pressure_by_topic,
            }
        }
    )

    return {
        "ended_at": ended_at,
        "accuracy": accuracy,
        "avg_time_spent_ms": avg_time,
        "practiced_topics": practiced_topics,
        "lectures_practised_count": lectures_practised_count,
        "topics_practised_count": topics_practised_count,
        "content_mastery_summaries": content_mastery_summaries,
        "focused_topic_mastery_summary": focused_topic_mastery_summary,
        "followup_topics_practised": followup_topics_practised,
        "followup_topic_mastery_summaries": followup_topic_mastery_summaries,
        "weakest_topic_this_session": weakest_topic,
        "strongest_topic_this_session": strongest_topic,
        "recommended_review_topic": recommended_review_topic,
        "recommended_review_topics": recommended_review_topics,
        "selected_content_ids": doc.get("selected_content_ids", []),
        "course_id": doc.get("course_id"),
        "recommendation": recommendation,
        "recommendation_code": (recommendation_payload or {}).get("code"),
        "recommendation_title": (recommendation_payload or {}).get("title"),
        "recommendation_text": (recommendation_payload or {}).get("text"),
        "session_origin": _normalize_session_origin(doc.get("session_origin")),
    }


def _serialize_session(doc: dict, include_questions: bool = False) -> dict:
    item = {
        "session_id": doc.get("session_id"),
        "course_id": doc.get("course_id"),
        "started_at": doc.get("started_at"),
        "ended_at": doc.get("ended_at"),
        "target_questions": doc.get("target_questions", 0),
        "questions_answered": doc.get("questions_answered", 0),
        "correct_answers": doc.get("correct_answers", 0),
        "accuracy": doc.get("accuracy", 0.0),
        "avg_time_spent_ms": doc.get("avg_time_spent_ms", 0),
        "total_time_spent_ms": doc.get("total_time_spent_ms", 0),
        "weakest_topic_this_session": doc.get("weakest_topic_this_session"),
        "strongest_topic_this_session": doc.get("strongest_topic_this_session"),
        "recommended_review_topic": (
            doc.get("recommended_review_topic")
            or doc.get("weakest_topic_this_session")
        ),
        "recommended_review_topics": (
            doc.get("recommended_review_topics")
            or ([doc.get("recommended_review_topic")] if doc.get("recommended_review_topic") else [])
        ),
        "recommendation": doc.get("recommendation"),
        "recommendation_code": doc.get("recommendation_code"),
        "recommendation_title": doc.get("recommendation_title"),
        "recommendation_text": doc.get("recommendation_text"),
        "end_difficulty": doc.get("end_difficulty", 3),
        "practiced_topics": doc.get("practiced_topics", []),
        "lectures_practised_count": doc.get("lectures_practised_count"),
        "topics_practised_count": doc.get("topics_practised_count"),
        "content_mastery_summaries": doc.get("content_mastery_summaries", []),
        "focused_topic_mastery_summary": doc.get("focused_topic_mastery_summary"),
        "followup_topics_practised": doc.get("followup_topics_practised", []),
        "followup_topic_mastery_summaries": doc.get("followup_topic_mastery_summaries", []),
        "selected_mode": doc.get("selected_mode", "normal_practice"),
        "session_origin": _normalize_session_origin(doc.get("session_origin")),
        "selected_content_ids": doc.get("selected_content_ids", []),
        "selected_content_titles": doc.get("selected_content_titles", []),
    }

    if include_questions:
        item["question_log"] = doc.get("question_log", [])

    return item


def _format_logged_answer(options: dict | None, answer_key: str | None) -> str:
    key = str(answer_key or "").strip()
    if not key:
        return "—"

    if not isinstance(options, dict):
        return key

    option_text = str(options.get(key) or "").strip()
    return f"{key} — {option_text}" if option_text else key


def _build_mistake_scope_key(parts: list[str]) -> str:
    normalized_parts = [str(part or "").strip() for part in parts if str(part or "").strip()]
    if not normalized_parts:
        normalized_parts = ["mistake-scope"]
    payload = "||".join(normalized_parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _build_session_content_refs(doc: dict, content_by_id: dict[str, dict]) -> list[dict]:
    selected_content_ids = [
        str(content_id or "").strip()
        for content_id in (doc.get("selected_content_ids") or [])
        if str(content_id or "").strip()
    ]
    selected_content_titles = [
        str(title or "").strip()
        for title in (doc.get("selected_content_titles") or [])
        if str(title or "").strip()
    ]

    refs: list[dict] = []

    for index, content_id in enumerate(selected_content_ids):
        content_doc = content_by_id.get(content_id) or {}
        fallback_title = selected_content_titles[index] if index < len(selected_content_titles) else ""
        refs.append({
            "content_id": content_id,
            "title": str(content_doc.get("title") or fallback_title or "Untitled Content").strip(),
            "week": content_doc.get("week"),
            "content_type": content_doc.get("content_type"),
            "topics": _dedupe_keep_order([
                str(topic or "").strip()
                for topic in (content_doc.get("topics") or [])
                if str(topic or "").strip()
            ]),
        })

    if refs or not selected_content_titles:
        return refs

    for index, title in enumerate(selected_content_titles):
        refs.append({
            "content_id": "",
            "title": title,
            "week": None,
            "content_type": None,
            "topics": [],
        })

    return refs


def _build_mistake_scope_label(content_refs: list[dict]) -> str:
    titles = _dedupe_keep_order([
        str(ref.get("title") or "").strip()
        for ref in (content_refs or [])
        if str(ref.get("title") or "").strip()
    ])

    if not titles:
        return "Selected content scope"
    if len(titles) == 1:
        return f"Selected content scope: {titles[0]}"
    return f"Selected content scope: {titles[0]} + {len(titles) - 1} more"


def _resolve_mistake_lecture_context(doc: dict, entry: dict, content_by_id: dict[str, dict]) -> dict:
    session_content_refs = _build_session_content_refs(doc, content_by_id)
    topic = str(entry.get("topic") or "").strip()

    matching_refs = [
        ref for ref in session_content_refs
        if topic and topic in (ref.get("topics") or [])
    ]

    if len(matching_refs) == 1:
        ref = matching_refs[0]
        content_id = str(ref.get("content_id") or "").strip()
        return {
            "lecture_key": f"content_{content_id}" if content_id else f"content_{_build_mistake_scope_key([ref.get('title')])}",
            "lecture_title": ref.get("title") or "Untitled Content",
            "lecture_scope_kind": "content",
            "lecture_week": ref.get("week"),
        }

    if len(matching_refs) > 1:
        return {
            "lecture_key": f"scope_{_build_mistake_scope_key([ref.get('content_id') or ref.get('title') for ref in matching_refs])}",
            "lecture_title": _build_mistake_scope_label(matching_refs),
            "lecture_scope_kind": "scope",
            "lecture_week": None,
        }

    if len(session_content_refs) == 1:
        ref = session_content_refs[0]
        content_id = str(ref.get("content_id") or "").strip()
        return {
            "lecture_key": f"content_{content_id}" if content_id else f"content_{_build_mistake_scope_key([ref.get('title')])}",
            "lecture_title": ref.get("title") or "Untitled Content",
            "lecture_scope_kind": "content" if content_id and not str(content_id).startswith("title_only_") else "scope",
            "lecture_week": ref.get("week"),
        }

    if session_content_refs:
        return {
            "lecture_key": f"scope_{_build_mistake_scope_key([ref.get('content_id') or ref.get('title') for ref in session_content_refs])}",
            "lecture_title": _build_mistake_scope_label(session_content_refs),
            "lecture_scope_kind": "scope",
            "lecture_week": None,
        }

    fallback_titles = _dedupe_keep_order([
        str(title or "").strip()
        for title in (doc.get("selected_content_titles") or [])
        if str(title or "").strip()
    ])
    if fallback_titles:
        return {
            "lecture_key": f"scope_{_build_mistake_scope_key(fallback_titles)}",
            "lecture_title": _build_mistake_scope_label([{"title": title} for title in fallback_titles]),
            "lecture_scope_kind": "scope",
            "lecture_week": None,
        }

    return {
        "lecture_key": "scope_unknown",
        "lecture_title": "Selected content scope",
        "lecture_scope_kind": "scope",
        "lecture_week": None,
    }


def _build_mistake_recovery_context(question_log: list[dict], question_index: int) -> dict | None:
    if question_index < 0 or question_index >= len(question_log):
        return None

    entry = question_log[question_index] or {}
    context: dict[str, object] = {}

    if entry.get("recovery_step_available"):
        context["guided_recovery_offered"] = True
        trigger_reason = str(entry.get("recovery_trigger_reason") or "").strip()
        if trigger_reason:
            context["trigger_reason"] = trigger_reason

    next_index = question_index + 1
    if next_index < len(question_log):
        next_entry = question_log[next_index] or {}
        same_topic = (
            str(next_entry.get("recovery_for_topic") or next_entry.get("topic") or "").strip()
            == str(entry.get("topic") or "").strip()
        )
        if next_entry.get("is_recovery_step") and same_topic:
            context["guided_recovery_used"] = True
            context["recovery_outcome"] = next_entry.get("recovery_outcome")

    return context or None


def _serialize_mistake_entry(doc: dict, entry: dict, question_index: int, question_log: list[dict]) -> dict:
    options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
    session_id = str(doc.get("session_id") or "").strip()
    session_timestamp = doc.get("ended_at") or doc.get("started_at")

    difficulty = entry.get("difficulty", 3)
    try:
        difficulty = int(difficulty)
    except (TypeError, ValueError):
        difficulty = 3

    return {
        "session_id": session_id,
        "session_ended_at": session_timestamp,
        "session_reference": f"Session {session_id[:8]}" if session_id else "Session",
        "question_id": entry.get("question_id"),
        "question_text": entry.get("question_text") or entry.get("question_id") or "Question unavailable",
        "topic": entry.get("topic") or "General",
        "options": options,
        "selected_answer": entry.get("selected_answer"),
        "selected_answer_text": _format_logged_answer(options, entry.get("selected_answer")),
        "correct_answer": entry.get("correct_answer"),
        "correct_answer_text": _format_logged_answer(options, entry.get("correct_answer")),
        "explanation": entry.get("explanation") or "",
        "difficulty": difficulty,
        "is_correct": False,
        "time_spent_ms": int(entry.get("time_spent_ms", 0) or 0),
        "recovery_context": _build_mistake_recovery_context(question_log, question_index),
    }


def _sort_mistake_entries(entries: list[dict]) -> list[dict]:
    return sorted(
        entries,
        key=lambda item: _parse_iso_datetime(item.get("session_ended_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def _serialize_mistake_journal_summary(groups: list[dict]) -> list[dict]:
    summaries: list[dict] = []

    for group in groups:
        summaries.append({
            "lecture_key": group.get("lecture_key"),
            "lecture_title": group.get("lecture_title"),
            "lecture_scope_kind": group.get("lecture_scope_kind"),
            "lecture_week": group.get("lecture_week"),
            "mistake_count": group.get("mistake_count", 0),
            "topic_count": group.get("topic_count", 0),
            "latest_at": group.get("latest_at"),
            "topics": [
                {
                    "topic": topic_group.get("topic"),
                    "mistake_count": topic_group.get("mistake_count", 0),
                    "latest_at": topic_group.get("latest_at"),
                }
                for topic_group in (group.get("topics") or [])
            ],
        })

    return summaries


async def _build_mistake_journal(student_id: str, course_id: str) -> list[dict]:
    db = get_db()
    content_docs = await db.course_content.find(
        {"course_id": course_id},
        {"_id": 1, "title": 1, "week": 1, "content_type": 1, "topics": 1}
    ).to_list(length=None)
    content_by_id = {
        str(doc.get("_id")): doc
        for doc in content_docs
        if doc.get("_id") is not None
    }

    cursor = db.student_session_history.find(
        {
            "student_id": student_id,
            "course_id": course_id,
            "ended_at": {"$ne": None},
        }
    ).sort("started_at", -1)

    grouped: dict[str, dict] = {}

    async for doc in cursor:
        question_log = doc.get("question_log") or []
        if not isinstance(question_log, list):
            continue

        session_timestamp = doc.get("ended_at") or doc.get("started_at")

        for question_index, entry in enumerate(question_log):
            if not isinstance(entry, dict):
                continue
            if entry.get("is_recovery_step"):
                continue
            if entry.get("counts_toward_session_score") is False:
                continue
            if entry.get("is_correct"):
                continue

            lecture_context = _resolve_mistake_lecture_context(doc, entry, content_by_id)
            lecture_key = lecture_context["lecture_key"]
            topic = str(entry.get("topic") or "").strip() or "General"
            group = grouped.setdefault(
                lecture_key,
                {
                    "lecture_key": lecture_key,
                    "lecture_title": lecture_context["lecture_title"],
                    "lecture_scope_kind": lecture_context["lecture_scope_kind"],
                    "lecture_week": lecture_context.get("lecture_week"),
                    "mistake_count": 0,
                    "latest_at": None,
                    "topics_by_name": {},
                },
            )

            group["mistake_count"] += 1
            if session_timestamp and (
                not group["latest_at"]
                or (_parse_iso_datetime(session_timestamp) or datetime.min.replace(tzinfo=timezone.utc))
                > (_parse_iso_datetime(group["latest_at"]) or datetime.min.replace(tzinfo=timezone.utc))
            ):
                group["latest_at"] = session_timestamp

            topic_group = group["topics_by_name"].setdefault(
                topic,
                {
                    "topic": topic,
                    "mistake_count": 0,
                    "latest_at": None,
                    "entries": [],
                },
            )
            topic_group["mistake_count"] += 1
            if session_timestamp and (
                not topic_group["latest_at"]
                or (_parse_iso_datetime(session_timestamp) or datetime.min.replace(tzinfo=timezone.utc))
                > (_parse_iso_datetime(topic_group["latest_at"]) or datetime.min.replace(tzinfo=timezone.utc))
            ):
                topic_group["latest_at"] = session_timestamp

            topic_group["entries"].append(
                _serialize_mistake_entry(
                    doc=doc,
                    entry=entry,
                    question_index=question_index,
                    question_log=question_log,
                )
            )

    groups: list[dict] = []
    for group in grouped.values():
        topic_groups = list(group.get("topics_by_name", {}).values())
        for topic_group in topic_groups:
            topic_group["entries"] = _sort_mistake_entries(topic_group.get("entries", []))

        topic_groups.sort(key=lambda topic_group: str(topic_group.get("topic") or "").lower())
        topic_groups.sort(
            key=lambda topic_group: _parse_iso_datetime(topic_group.get("latest_at")) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        topic_groups.sort(key=lambda topic_group: int(topic_group.get("mistake_count", 0)), reverse=True)

        groups.append({
            "lecture_key": group.get("lecture_key"),
            "lecture_title": group.get("lecture_title"),
            "lecture_scope_kind": group.get("lecture_scope_kind"),
            "lecture_week": group.get("lecture_week"),
            "mistake_count": group.get("mistake_count", 0),
            "topic_count": len(topic_groups),
            "latest_at": group.get("latest_at"),
            "topics": topic_groups,
        })

    groups.sort(key=lambda group: str(group.get("lecture_title") or "").lower())
    groups.sort(
        key=lambda group: _parse_iso_datetime(group.get("latest_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    groups.sort(key=lambda group: int(group.get("mistake_count", 0)), reverse=True)
    return groups

async def _get_cached_question(
    topic: str,
    difficulty: int,
    course_id: str,
    source_scope_key: str,
    prompt_version: str,
    excluded_hashes: list[str] | None = None,
    allow_relaxed_fallback: bool = True,
) -> dict | None:
    db = get_db()
    now = datetime.now(timezone.utc)
    used_at = now.isoformat()
    expires_at = _cache_used_expires_at(now)

    async def _claim(query: dict) -> dict | None:
        return await db.questions_cache.find_one_and_update(
            query,
            {"$set": {"used": True, "used_at": used_at, "expires_at": expires_at}},
            sort=[("generated_at", -1)],
        )

    base_query = {
        "topic": topic,
        "difficulty": difficulty,
        "course_id": course_id,
        "source_scope_key": source_scope_key,
        "prompt_version": prompt_version,
        "used": False,
    }

    question = None
    if excluded_hashes:
        question = await _claim({
            **base_query,
            "question_hash": {"$nin": excluded_hashes},
        })
        if question:
            logger.info(
                "[DEDUPE] Cache preferred course=%s topic=%s difficulty=%s",
                course_id,
                topic,
                difficulty,
            )
        elif allow_relaxed_fallback:
            question = await _claim(base_query)
            if question:
                logger.info(
                    "[DEDUPE] Cache relaxed course=%s topic=%s difficulty=%s",
                    course_id,
                    topic,
                    difficulty,
                )
    else:
        question = await _claim(base_query)

    if question:
        question["used"] = True
        question["used_at"] = used_at
        question["expires_at"] = expires_at
        logger.info(
            "[CACHE] TTL set used topic=%s difficulty=%s expires_at=%s",
            topic,
            difficulty,
            expires_at.isoformat(),
        )
        logger.info(
            "[CACHE] Hit topic=%s difficulty=%s prompt_version=%s",
            topic, difficulty, prompt_version
        )
        question.pop("_id", None)
    return question


async def _replenish_cache(
    topic: str,
    difficulty: int,
    course_id: str,
    source_text: str,
    source_scope_key: str,
    prompt_version: str,
    target: int = DEFAULT_CACHE_TARGET,
):
    bucket_key = _cache_bucket_key(
        topic=topic,
        difficulty=difficulty,
        course_id=course_id,
        source_scope_key=source_scope_key,
        prompt_version=prompt_version,
    )
    if bucket_key in _REPLENISH_IN_FLIGHT:
        logger.info(
            "[CACHE] Replenish skipped inflight topic=%s difficulty=%s",
            topic, difficulty
        )
        return

    _REPLENISH_IN_FLIGHT.add(bucket_key)
    db = get_db()
    try:
        count = await db.questions_cache.count_documents({
            "topic": topic,
            "difficulty": difficulty,
            "course_id": course_id,
            "source_scope_key": source_scope_key,
            "prompt_version": prompt_version,
            "used": False
        })
        needed = max(0, target - count)

        logger.info(
            "[CACHE] Replenish start topic=%s difficulty=%s current_unused=%s target=%s needed=%s",
            topic, difficulty, count, target, needed
        )

        for _ in range(needed):
            try:
                await asyncio.sleep(2)
                q = await generate_question(topic, difficulty, source_text)
                now = datetime.now(timezone.utc)
                generated_at = now.isoformat()
                expires_at = _cache_unused_expires_at(now)
                q["course_id"] = course_id
                q["used"] = False
                q["generated_at"] = generated_at
                q["used_at"] = None
                q["expires_at"] = expires_at
                q["source_scope_key"] = source_scope_key
                q["prompt_version"] = prompt_version
                q["question_hash"] = _make_question_hash(q)

                result = await db.questions_cache.insert_one(q)

                logger.info(
                    "[CACHE] TTL set unused topic=%s difficulty=%s expires_at=%s",
                    topic,
                    difficulty,
                    expires_at.isoformat(),
                )
                logger.info(
                    "[CACHE] Replenish inserted topic=%s difficulty=%s cache_id=%s",
                    topic, difficulty, str(result.inserted_id)
                )

            except Exception as e:
                logger.warning(
                    "[CACHE] Replenish failed topic=%s difficulty=%s error=%r",
                    topic, difficulty, e
                )
                break
    finally:
        _REPLENISH_IN_FLIGHT.discard(bucket_key)
        
def _serialize_content_item(doc: dict, include_source_text: bool = False) -> dict:
    item = {
        "id": str(doc["_id"]),
        "course_id": doc.get("course_id"),
        "course_name": doc.get("course_name"),
        "week": doc.get("week"),
        "content_type": doc.get("content_type"),
        "title": doc.get("title"),
        "topics": doc.get("topics", []),
        "active": doc.get("active", True),
        "source_version": doc.get("updated_at") or doc.get("uploaded_at") or str(doc["_id"]),
    }
    if include_source_text:
        item["source_text"] = doc.get("source_text", "")
    return item


def _parse_object_id(content_id: str) -> ObjectId:
    try:
        return ObjectId(content_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid content ID.")
    
async def _generate_first_question(
    state: dict,
    student_id: str,
    course_id: str,
    topic: str,
    difficulty: int,
    source_text: str,
    session_id: str,
    max_questions: int,
) -> dict:
    """Generate (or pull from cache) the first question for a freshly created session."""
    source_scope_key = _make_source_scope_key(source_text)
    recent_hashes = _recent_seen_hashes_from_state(state, course_id)
    cache_target = _cache_target_for_state(state, topic, difficulty)
    preferred_only = _state_is_focused_followup(state)
    cached = await _get_cached_question(
        topic=topic,
        difficulty=difficulty,
        course_id=course_id,
        source_scope_key=source_scope_key,
        prompt_version=CACHE_PROMPT_VERSION,
        excluded_hashes=recent_hashes,
        allow_relaxed_fallback=not preferred_only,
    )
    if cached:
        asyncio.create_task(
            _replenish_cache(
                topic=topic,
                difficulty=difficulty,
                course_id=course_id,
                source_text=source_text,
                source_scope_key=source_scope_key,
                prompt_version=CACHE_PROMPT_VERSION,
                target=cache_target,
            )
        )
        question = cached
    else:
        question = None
        if _state_is_focused_followup(state):
            waited_for_cache = await _wait_for_replenished_cache(
                topic=topic,
                difficulty=difficulty,
                course_id=course_id,
                source_scope_key=source_scope_key,
                prompt_version=CACHE_PROMPT_VERSION,
            )
            if waited_for_cache:
                logger.info(
                    "[FOLLOWUP] Retrying preferred cache after wait topic=%s difficulty=%s",
                    topic,
                    difficulty,
                )
                question = await _get_cached_question(
                    topic=topic,
                    difficulty=difficulty,
                    course_id=course_id,
                    source_scope_key=source_scope_key,
                    prompt_version=CACHE_PROMPT_VERSION,
                    excluded_hashes=recent_hashes,
                    allow_relaxed_fallback=False,
                )
                if question:
                    logger.info(
                        "[FOLLOWUP] Cache recovered after wait topic=%s difficulty=%s",
                        topic,
                        difficulty,
                    )
                else:
                    logger.info(
                        "[FOLLOWUP] Preferred cache unavailable after wait topic=%s difficulty=%s",
                        topic,
                        difficulty,
                    )
                    logger.info(
                        "[FOLLOWUP] Trying relaxed cache after wait topic=%s difficulty=%s",
                        topic,
                        difficulty,
                    )
                    question = await _get_cached_question(
                        topic=topic,
                        difficulty=difficulty,
                        course_id=course_id,
                        source_scope_key=source_scope_key,
                        prompt_version=CACHE_PROMPT_VERSION,
                        excluded_hashes=None,
                    )
                    if question:
                        logger.info(
                            "[FOLLOWUP] Relaxed cache recovered after wait topic=%s difficulty=%s",
                            topic,
                            difficulty,
                        )

            if question:
                asyncio.create_task(
                    _replenish_cache(
                        topic=topic,
                        difficulty=difficulty,
                        course_id=course_id,
                        source_text=source_text,
                        source_scope_key=source_scope_key,
                        prompt_version=CACHE_PROMPT_VERSION,
                        target=cache_target,
                    )
                )

        if question is None:
            if _state_is_focused_followup(state):
                logger.info(
                    "[FOLLOWUP] Live generation only after relaxed cache miss topic=%s difficulty=%s",
                    topic,
                    difficulty,
                )
            logger.info(
                "[CACHE] Miss topic=%s requested_difficulty=%s prompt_version=%s",
                topic, difficulty, CACHE_PROMPT_VERSION
            )
            try:
                question = await _generate_question_with_soft_dedupe(
                    topic=topic,
                    difficulty=difficulty,
                    source_text=source_text,
                    course_id=course_id,
                    recent_hashes=recent_hashes,
                )
                generated_difficulty = int(question.get("difficulty", difficulty))
                logger.info(
                    "[CACHE] Warm on miss topic=%s requested_difficulty=%s generated_difficulty=%s",
                    topic,
                    difficulty,
                    generated_difficulty,
                )
                asyncio.create_task(
                    _replenish_cache(
                        topic=topic,
                        difficulty=generated_difficulty,
                        course_id=course_id,
                        source_text=source_text,
                        source_scope_key=source_scope_key,
                        prompt_version=CACHE_PROMPT_VERSION,
                        target=cache_target,
                    )
                )
            except ValueError as e:
                return {"success": False, "error": str(e)}

    _record_seen_question(
        state=state,
        question=question,
        course_id=course_id,
        topic=str(question.get("topic", topic)),
        difficulty=int(question.get("difficulty", difficulty)),
        source_scope_key=source_scope_key,
        prompt_version=CACHE_PROMPT_VERSION,
    )
    await _save_state(state)

    return {
        "success":          True,
        "question":         question,
        "questions_seen":   0,
        "max_questions":    max_questions,
        "current_difficulty": difficulty,
        "session_id":       session_id,
    }
    
def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _apply_focus_topics(
    resolved_topics: list[str],
    focus_topics: list[str] | None,
) -> tuple[list[str], list[str], str | None]:
    normalized_focus_topics = _dedupe_keep_order([
        str(topic).strip()
        for topic in (focus_topics or [])
        if str(topic).strip()
    ])
    if not normalized_focus_topics:
        return resolved_topics, [], None

    valid_focus_topics = [
        topic for topic in normalized_focus_topics
        if topic in resolved_topics
    ][:2]
    if not valid_focus_topics:
        return resolved_topics, [], None

    return valid_focus_topics, valid_focus_topics, valid_focus_topics[0]


def _set_session_followup_state(
    state: dict,
    mode: str,
    focus_topics: list[str] | None,
    focused_topic: str | None,
    session_origin: str | None,
) -> bool:
    normalized_origin = _normalize_session_origin(session_origin)
    is_focused_followup = (
        _is_focused_followup_session(mode, focus_topics, normalized_origin)
        and focused_topic is not None
    )
    state["current_session_origin"] = normalized_origin
    state["current_is_focused_followup"] = is_focused_followup
    state["current_focus_topic"] = focused_topic if is_focused_followup else None

    if is_focused_followup:
        logger.info(
            "[FOLLOWUP] Focused follow-up detected topic=%s mode=%s origin=%s",
            focused_topic,
            mode,
            normalized_origin,
        )

    return is_focused_followup

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate(req: GenerateRequest):
    """Generate next question — serve from cache if available."""
    try:
        state = await _get_state(req.student_id, req.course_id, [req.topic])
        source_scope_key = state.get("current_source_scope_key") or _make_source_scope_key(req.source_text)
        prompt_version = state.get("current_cache_prompt_version") or CACHE_PROMPT_VERSION
        recent_hashes = _recent_seen_hashes_from_state(state, req.course_id)
        cache_target = _cache_target_for_state(state, req.topic, req.difficulty)
        preferred_only = _state_is_focused_followup(state)

        # Try cache first
        cached = await _get_cached_question(
            topic=req.topic,
            difficulty=req.difficulty,
            course_id=req.course_id,
            source_scope_key=source_scope_key,
            prompt_version=prompt_version,
            excluded_hashes=recent_hashes,
            allow_relaxed_fallback=not preferred_only,
        )
        if cached:
            # Fire background replenishment
            asyncio.create_task(
                _replenish_cache(
                    topic=req.topic,
                    difficulty=req.difficulty,
                    course_id=req.course_id,
                    source_text=req.source_text,
                    source_scope_key=source_scope_key,
                    prompt_version=prompt_version,
                    target=cache_target,
                )
            )
            _record_seen_question(
                state=state,
                question=cached,
                course_id=req.course_id,
                topic=str(cached.get("topic", req.topic)),
                difficulty=int(cached.get("difficulty", req.difficulty)),
                source_scope_key=source_scope_key,
                prompt_version=prompt_version,
            )
            await _save_state(state)
            return cached

        if _state_is_focused_followup(state):
            waited_for_cache = await _wait_for_replenished_cache(
                topic=req.topic,
                difficulty=req.difficulty,
                course_id=req.course_id,
                source_scope_key=source_scope_key,
                prompt_version=prompt_version,
            )
            recovered = None
            if waited_for_cache:
                logger.info(
                    "[FOLLOWUP] Retrying preferred cache after wait topic=%s difficulty=%s",
                    req.topic,
                    req.difficulty,
                )
                recovered = await _get_cached_question(
                    topic=req.topic,
                    difficulty=req.difficulty,
                    course_id=req.course_id,
                    source_scope_key=source_scope_key,
                    prompt_version=prompt_version,
                    excluded_hashes=recent_hashes,
                    allow_relaxed_fallback=False,
                )
                if recovered:
                    logger.info(
                        "[FOLLOWUP] Cache recovered after wait topic=%s difficulty=%s",
                        req.topic,
                        req.difficulty,
                    )
                else:
                    logger.info(
                        "[FOLLOWUP] Preferred cache unavailable after wait topic=%s difficulty=%s",
                        req.topic,
                        req.difficulty,
                    )
                    logger.info(
                        "[FOLLOWUP] Trying relaxed cache after wait topic=%s difficulty=%s",
                        req.topic,
                        req.difficulty,
                    )
                    recovered = await _get_cached_question(
                        topic=req.topic,
                        difficulty=req.difficulty,
                        course_id=req.course_id,
                        source_scope_key=source_scope_key,
                        prompt_version=prompt_version,
                        excluded_hashes=None,
                    )
                    if recovered:
                        logger.info(
                            "[FOLLOWUP] Relaxed cache recovered after wait topic=%s difficulty=%s",
                            req.topic,
                            req.difficulty,
                        )

            if recovered:
                asyncio.create_task(
                    _replenish_cache(
                        topic=req.topic,
                        difficulty=req.difficulty,
                        course_id=req.course_id,
                        source_text=req.source_text,
                        source_scope_key=source_scope_key,
                        prompt_version=prompt_version,
                        target=cache_target,
                    )
                )
                _record_seen_question(
                    state=state,
                    question=recovered,
                    course_id=req.course_id,
                    topic=str(recovered.get("topic", req.topic)),
                    difficulty=int(recovered.get("difficulty", req.difficulty)),
                    source_scope_key=source_scope_key,
                    prompt_version=prompt_version,
                )
                await _save_state(state)
                return recovered

        # Cache miss — live generation
        if _state_is_focused_followup(state):
            logger.info(
                "[FOLLOWUP] Live generation only after relaxed cache miss topic=%s difficulty=%s",
                req.topic,
                req.difficulty,
            )
        logger.info(
            "[CACHE] Miss topic=%s requested_difficulty=%s prompt_version=%s",
            req.topic, req.difficulty, prompt_version
        )
        question = await _generate_question_with_soft_dedupe(
            topic=req.topic,
            difficulty=req.difficulty,
            source_text=req.source_text,
            course_id=req.course_id,
            recent_hashes=recent_hashes,
        )
        generated_difficulty = int(question.get("difficulty", req.difficulty))
        logger.info(
            "[CACHE] Warm on miss topic=%s requested_difficulty=%s generated_difficulty=%s",
            req.topic,
            req.difficulty,
            generated_difficulty,
        )
        asyncio.create_task(
            _replenish_cache(
                topic=req.topic,
                difficulty=generated_difficulty,
                course_id=req.course_id,
                source_text=req.source_text,
                source_scope_key=source_scope_key,
                prompt_version=prompt_version,
                target=cache_target,
            )
        )
        _record_seen_question(
            state=state,
            question=question,
            course_id=req.course_id,
            topic=str(question.get("topic", req.topic)),
            difficulty=generated_difficulty,
            source_scope_key=source_scope_key,
            prompt_version=prompt_version,
        )
        await _save_state(state)
        return question

    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/submit", response_model=SubmitResponse)
async def submit(req: SubmitRequest):
    """Submit an answer, update mastery, log session analytics, return next parameters."""
    is_correct = req.selected_answer == req.correct_answer

    # Load state from MongoDB
    state = await _get_state(req.student_id, req.course_id, [req.topic])
    current_session_origin = _normalize_session_origin(state.get("current_session_origin"))
    current_mode = state.get("current_session_mode", "normal_practice")
    db = get_db()
    session_doc_before = None

    if req.topic not in state["topic_mastery"]:
        state["topic_mastery"][req.topic] = 0.5
    normalized_confidence = normalize_confidence_level(req.confidence)

    if req.session_id:
        session_doc_before = await db.student_session_history.find_one({"session_id": req.session_id})

    # Run adaptive logic
    state = process_answer(
        state=state,
        topic=req.topic,
        correct=is_correct,
        time_ms=req.time_spent_ms,
        time_context=req.time_context,
        confidence=normalized_confidence,
    )

    # Decide next parameters (scoped to current session topics)
    allowed_topics = state.get("session_topics") or list(state["topic_mastery"].keys()) or [req.topic]

    scoped_mastery = {
        topic: state["topic_mastery"].get(topic, 0.5)
        for topic in allowed_topics
    }

    current_mode = state.get("current_session_mode", "normal_practice")
    next_topic, next_mode = select_next_topic(
        scoped_mastery,
        mode=current_mode,
        recent_answers=state.get("recent_answers", []),
        total_answers=state.get("total_answers", 0),
    )

    next_topic_mastery = state["topic_mastery"].get(next_topic, 0.5)
    next_difficulty = select_difficulty(next_topic_mastery)
    state["current_difficulty"] = next_difficulty

    updated_mastery = state["topic_mastery"].get(req.topic, 0.5)

    # Support features
    support_features = []
    recent = state["recent_answers"]

    consecutive_wrong = 0
    for ans in reversed(recent):
        if not ans["correct"]:
            consecutive_wrong += 1
        else:
            break

    narrative_bridge = _build_narrative_bridge(
        is_correct=is_correct,
        current_topic=req.topic,
        next_topic=next_topic,
        next_mode=next_mode,
        session_origin=current_session_origin,
        current_difficulty=req.difficulty,
        next_difficulty=next_difficulty,
        consecutive_wrong=consecutive_wrong,
        current_topic_mastery=state["topic_mastery"].get(req.topic, 0.5),
        next_topic_mastery=state["topic_mastery"].get(next_topic, 0.5),
    )

    support_features.append("explain_simpler")
    support_features.append("one_more_like_this")

    if not is_correct:
        # Always show normal explanation and offer a simpler version immediately
        support_features.append("explanation")

    session_complete = False
    session_summary = {}
    recovery_step_available = False
    recovery_reason = None
    recovery_topic = None
    recovery_message = None

    if req.session_id:
        prior_question_log = (session_doc_before or {}).get("question_log", [])
        answered_after_submission = int((session_doc_before or {}).get("questions_answered", 0)) + 1
        target_questions = int((session_doc_before or {}).get("target_questions", 10))
        recovery_reason = _detect_recovery_trigger(
            current_mode=current_mode,
            session_origin=current_session_origin,
            session_complete=answered_after_submission >= target_questions,
            is_correct=is_correct,
            topic=req.topic,
            time_spent_ms=req.time_spent_ms,
            time_context=req.time_context,
            confidence=normalized_confidence,
            question_log=prior_question_log,
        )
        question_entry = {
            "question_id": req.question_id,
            "question_text": req.question_text or req.question_id,
            "options": req.options or {},
            "explanation": req.explanation or "",
            "topic": req.topic,
            "difficulty": req.difficulty,
            "selected_answer": req.selected_answer,
            "correct_answer": req.correct_answer,
            "is_correct": is_correct,
            "time_spent_ms": req.time_spent_ms,
            "time_context": _normalize_recovery_time_context(req.time_context) or "unknown",
            "confidence": normalized_confidence,
            "support_features_shown": support_features,
            "is_recovery_step": False,
            "counts_toward_session_score": True,
            "recovery_step_available": bool(recovery_reason),
            "recovery_trigger_reason": recovery_reason,
        }

        await _append_question_log(
            session_id=req.session_id,
            question_entry=question_entry,
            is_correct=is_correct,
            time_spent_ms=req.time_spent_ms,
            question_difficulty=req.difficulty,
            end_difficulty=next_difficulty,
            counts_toward_session=True,
        )

        session_doc = await db.student_session_history.find_one({"session_id": req.session_id})
        if session_doc:
            answered = session_doc.get("questions_answered", 0)
            target = session_doc.get("target_questions", 10)
            session_complete = answered >= target

            if session_complete:
                _clear_recovery_state(state)
                topic_mastery_after = {
                    topic: state["topic_mastery"].get(topic, 0.5)
                    for topic in allowed_topics
                }
                session_summary = await _finalize_session_history(req.session_id, topic_mastery_after)
            else:
                _clear_recovery_state(state)
                if recovery_reason:
                    _set_pending_recovery_offer(
                        state,
                        session_id=req.session_id,
                        topic=req.topic,
                        difficulty=req.difficulty,
                        trigger_reason=recovery_reason,
                        question_id=req.question_id,
                    )
                    recovery_step_available = True
                    recovery_topic = req.topic
                    recovery_message = RECOVERY_SUPPORT_MESSAGE
        else:
            _clear_recovery_state(state)
    else:
        _clear_recovery_state(state)

    await _save_state(state)

    return SubmitResponse(
        is_correct=is_correct,
        explanation="See the question explanation field.",
        updated_mastery=updated_mastery,
        next_difficulty=next_difficulty,
        next_topic=next_topic,
        next_mode=next_mode,
        support_features=support_features,
        session_complete=session_complete,
        session_recommendation=session_summary.get("recommendation"),
        recommendation_code=session_summary.get("recommendation_code"),
        recommendation_title=session_summary.get("recommendation_title"),
        recommendation_text=session_summary.get("recommendation_text"),
        weakest_topic_this_session=session_summary.get("weakest_topic_this_session"),
        strongest_topic_this_session=session_summary.get("strongest_topic_this_session"),
        session_accuracy=session_summary.get("accuracy"),
        avg_time_spent_ms=session_summary.get("avg_time_spent_ms"),
        lectures_practised_count=session_summary.get("lectures_practised_count"),
        topics_practised_count=session_summary.get("topics_practised_count"),
        content_mastery_summaries=session_summary.get("content_mastery_summaries"),
        recommended_review_topic=session_summary.get("recommended_review_topic"),
        recommended_review_topics=session_summary.get("recommended_review_topics"),
        selected_content_ids=session_summary.get("selected_content_ids"),
        course_id=session_summary.get("course_id"),
        session_origin=session_summary.get("session_origin", current_session_origin),
        focused_topic_mastery_summary=session_summary.get("focused_topic_mastery_summary"),
        followup_topics_practised=session_summary.get("followup_topics_practised"),
        followup_topic_mastery_summaries=session_summary.get("followup_topic_mastery_summaries"),
        narrative_bridge=narrative_bridge,
        recovery_step_available=recovery_step_available,
        recovery_message=recovery_message,
        recovery_reason=recovery_reason,
        recovery_topic=recovery_topic,
    )


@router.get("/state/{student_id}/{course_id}")
async def get_state(student_id: str, course_id: str):
    """Fetch current adaptive state for a student."""
    db = get_db()
    state = await db.student_states.find_one(
        _state_key(student_id, course_id),
        {"_id": 0}
    )
    if not state:
        raise HTTPException(status_code=404, detail="Student state not found")

    stats = await _compute_overall_stats(student_id, course_id)
    state["completed_sessions"] = stats["completed_sessions"]
    state["completed_questions_answered"] = stats["completed_questions_answered"]
    state["completed_correct_answers"] = stats["completed_correct_answers"]
    state["overall_accuracy"] = stats["overall_accuracy"]
    state["overall_avg_time_spent_ms"] = stats["overall_avg_time_spent_ms"]

    return state


@router.get("/mastery/{student_id}/{course_id}", response_model=MasteryResponse)
async def get_mastery(student_id: str, course_id: str):
    """Full mastery breakdown for progress dashboard."""
    db = get_db()
    state = await db.student_states.find_one(
        _state_key(student_id, course_id),
        {"_id": 0}
    )
    if not state:
        raise HTTPException(status_code=404, detail="Student state not found")

    mastery = state["topic_mastery"]
    topic_labels = {
        topic: _mastery_label(value)
        for topic, value in mastery.items()
    }

    weak_topics = [
    t for t, lbl in topic_labels.items()
    if lbl in {"Struggling", "Emerging"}
    ]

    strong_topics = [
        t for t, lbl in topic_labels.items()
        if lbl in {"Proficient", "Mastered"}
    ]

    return MasteryResponse(
        student_id=student_id,
        course_id=course_id,
        topic_mastery=mastery,
        topic_labels=topic_labels,
        weak_topics=weak_topics,
        strong_topics=strong_topics
    )

@router.post("/session/start")
async def session_start(req: GenerateRequest):
    """
    Resolve content, check diagnostic status, and decide what happens next.

    If all selected content items have already been diagnosed at their current
    version -> create session immediately and return session metadata.

    If any item is new or has been updated since last diagnostic ->
    do NOT create session_history yet. Return diagnostic metadata only.
    The XBlock runs the diagnostic, then calls /session/finalize.
    """
    db = get_db()

    if not req.content_ids:
        raise HTTPException(
            status_code=422,
            detail="Please select at least one content item.",
        )

    requested_mode = (
        req.mode
        if req.mode in {"auto", "normal_practice", "weakness_review", "challenge"}
        else "normal_practice"
    )
    requested_session_origin = _normalize_session_origin(req.session_origin)

    # Resolve selected content items by real Mongo _id
    resolved_topics: list[str] = []
    resolved_source_text: str = ""
    diagnostic_items: list[dict] = []
    resolved_content_titles: list[str] = []

    for content_id in req.content_ids:
        try:
            oid = ObjectId(content_id)
        except Exception:
            continue

        item = await db.course_content.find_one({
            "_id": oid,
            "course_id": req.course_id,
            "active": True,
        })
        if not item:
            continue

        topics = item.get("topics", [])
        source_text = item.get("source_text", "")
        source_version = (
            item.get("updated_at")
            or item.get("uploaded_at")
            or str(item["_id"])
        )

        resolved_topics.extend(topics)
        resolved_source_text += "\n\n" + source_text
        resolved_content_titles.append(item.get("title", ""))

        target_questions = diagnostic_target_question_count(len(topics))
        coverage_goal = diagnostic_coverage_goal(len(topics), target_questions)

        diagnostic_items.append({
            "content_id": str(item["_id"]),
            "title": item.get("title", ""),
            "topics": topics,
            "source_text": source_text,
            "source_version": source_version,
            "diagnostic_target_questions": target_questions,
            "diagnostic_coverage_goal": coverage_goal,
        })

    # Deduplicate topics while preserving order
    seen = set()
    deduped_topics = []
    for topic in resolved_topics:
        if topic not in seen:
            seen.add(topic)
            deduped_topics.append(topic)
    resolved_topics = deduped_topics
    resolved_content_titles = _dedupe_keep_order(resolved_content_titles)
    source_scope_key = _make_source_scope_key(resolved_source_text)

    if not resolved_topics:
        raise HTTPException(
            status_code=404,
            detail="No active content found for the selected items.",
        )

    scoped_topics = list(resolved_topics)
    resolved_topics, applied_focus_topics, focused_topic = _apply_focus_topics(resolved_topics, req.focus_topics)
    if applied_focus_topics:
        logger.info(
            "[SESSION] Focus topics applied topics=%s mode=%s",
            applied_focus_topics,
            requested_mode,
        )
    elif req.focus_topics:
        logger.info(
            "[SESSION] Focus topic unavailable requested=%s mode=%s",
            req.focus_topics,
            requested_mode,
        )

    # Load or create state
    state = await _get_state(req.student_id, req.course_id, resolved_topics)

    if "topic_mastery_source" not in state:
        state["topic_mastery_source"] = {}

    for topic in resolved_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5
        if topic not in state["topic_mastery_source"]:
            state["topic_mastery_source"][topic] = "default_prior"

    for topic in scoped_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5
        if topic not in state["topic_mastery_source"]:
            state["topic_mastery_source"][topic] = "default_prior"

    if requested_mode == "challenge":
        challenge_readiness = _build_challenge_readiness(
            topic_mastery=state.get("topic_mastery", {}),
            scoped_topics=scoped_topics,
        )
        if not challenge_readiness["ready"]:
            await _save_state(state)
            logger.info(
                "[CHALLENGE] Blocked start course=%s topics=%s avg=%.3f proficient=%s/%s",
                req.course_id,
                len(scoped_topics),
                challenge_readiness["avg_mastery"],
                challenge_readiness["proficient_topic_count"],
                challenge_readiness["required_proficient_topics"],
            )
            return _challenge_not_ready_response(challenge_readiness)

    # Determine which selected items still need diagnostic
    items_needing_diagnostic = [
        item for item in diagnostic_items
        if not is_content_diagnosed(
            state,
            make_content_key(item["content_id"], item["source_version"])
        )
    ]
    diagnostic_needed = len(items_needing_diagnostic) > 0

    # Pick starting topic order + difficulty for this session scope
    scoped_mastery = {
        topic: state["topic_mastery"].get(topic, 0.5)
        for topic in resolved_topics
    }

    start_topic, effective_mode = select_next_topic(
        scoped_mastery,
        mode=requested_mode,
        recent_answers=state.get("recent_answers", []),
        total_answers=state.get("total_answers", 0),
    )

    resolved_topics = [start_topic] + [t for t in resolved_topics if t != start_topic]

    state["session_topics"] = resolved_topics
    state["current_session_mode"] = requested_mode
    _clear_recovery_state(state)
    is_focused_followup = _set_session_followup_state(
        state=state,
        mode=requested_mode,
        focus_topics=applied_focus_topics,
        focused_topic=focused_topic,
        session_origin=requested_session_origin,
    )
    state["current_source_scope_key"] = source_scope_key
    state["current_cache_prompt_version"] = CACHE_PROMPT_VERSION
    state["current_difficulty"] = select_session_start_difficulty(
        state.get("topic_mastery", {}),
        resolved_topics,
        mode=requested_mode,
    )

    await _save_state(state)

    if diagnostic_needed:
        return {
            "diagnostic_needed": True,
            "diagnostic_items": items_needing_diagnostic,
            "diagnostic_questions_per_item": max(
                (item["diagnostic_target_questions"] for item in items_needing_diagnostic),
                default=0,
            ),
            "all_content_ids": req.content_ids,
            "question_count": req.question_count,
            "resolved_source_text": resolved_source_text,
            "topics": resolved_topics,
            "selected_mode": requested_mode,
            "session_origin": requested_session_origin,
            "effective_mode": effective_mode,
            "current_difficulty": state["current_difficulty"],
        }

    # No diagnostic needed -> create session immediately
    session_id = await _create_session_history(
        student_id=req.student_id,
        course_id=req.course_id,
        session_topics=resolved_topics,
        mastery_before={t: state["topic_mastery"].get(t, 0.5) for t in resolved_topics},
        start_difficulty=state["current_difficulty"],
        target_questions=req.question_count,
        selected_content_ids=req.content_ids,
        selected_content_titles=resolved_content_titles,
        selected_mode=requested_mode,
        session_origin=requested_session_origin,
    )

    state["session_count"] = state.get("session_count", 0) + 1
    await _save_state(state)

    if is_focused_followup:
        cache_target = _cache_target_for_state(
            state,
            resolved_topics[0],
            state["current_difficulty"],
        )
        logger.info(
            "[FOLLOWUP] Eager prefill started topic=%s difficulty=%s",
            resolved_topics[0],
            state["current_difficulty"],
        )
        asyncio.create_task(
            _replenish_cache(
                topic=resolved_topics[0],
                difficulty=state["current_difficulty"],
                course_id=req.course_id,
                source_text=resolved_source_text,
                source_scope_key=source_scope_key,
                prompt_version=CACHE_PROMPT_VERSION,
                target=cache_target,
            )
        )

    return {
        "diagnostic_needed": False,
        "session_id": session_id,
        "topics": resolved_topics,
        "resolved_source_text": resolved_source_text,
        "current_difficulty": state["current_difficulty"],
        "topic_mastery": state["topic_mastery"],
        "irt_active": state["irt_active"],
        "cache_filling": False,
        "selected_mode": requested_mode,
        "session_origin": requested_session_origin,
        "effective_mode": effective_mode,
    }


async def _generate_diagnostic_question_with_fallback(
    topic: str,
    difficulty: int,
    source_text: str,
) -> dict:
    async def _attempt_with_budget(target_difficulty: int) -> tuple[str, dict | None, dict | None]:
        try:
            question, metadata = await generate_question_with_metadata(
                topic=topic,
                difficulty=target_difficulty,
                source_text=source_text,
                max_provider_model_attempts=DIAGNOSTIC_FAST_PATH_MAX_ATTEMPTS,
                allow_internal_fallback=False,
                generation_profile="diagnostic",
            )
        except ValueError:
            logger.info(
                "[DIAG] Planned difficulty budget exhausted topic=%s difficulty=%s",
                topic,
                target_difficulty,
            )
            return "failure", None, None

        is_clean = not metadata.get("used_last_resort")
        logger.info(
            "[DIAG] Diagnostic fast-path success topic=%s difficulty=%s clean=%s",
            topic,
            int(question.get("difficulty", target_difficulty)),
            is_clean,
        )
        return ("clean" if is_clean else "reserve"), question, metadata

    planned_outcome, planned_question, planned_metadata = await _attempt_with_budget(int(difficulty))
    if planned_outcome == "clean" and planned_question is not None:
        return planned_question

    lower_difficulty = max(1, int(difficulty) - 1)
    if lower_difficulty < int(difficulty):
        retry_reason = "last_resort" if planned_outcome == "reserve" else "budget_exhausted"
        logger.info(
            "[DIAG] Retrying lower difficulty topic=%s from=%s to=%s reason=%s",
            topic,
            difficulty,
            lower_difficulty,
            retry_reason,
        )
        lower_outcome, lower_question, lower_metadata = await _attempt_with_budget(lower_difficulty)
        if lower_outcome == "clean" and lower_question is not None:
            logger.info(
                "[DIAG] Lower-difficulty fallback succeeded topic=%s difficulty=%s",
                topic,
                int(lower_question.get("difficulty", lower_difficulty)),
            )
            return lower_question
        if lower_outcome == "reserve" and lower_question is not None:
            logger.info(
                "[DIAG] Using reserve candidate topic=%s difficulty=%s source=%s",
                topic,
                int(lower_question.get("difficulty", lower_difficulty)),
                (lower_metadata or {}).get("source", "unknown"),
            )
            return lower_question

    if planned_outcome == "reserve" and planned_question is not None:
        logger.info(
            "[DIAG] Using reserve candidate topic=%s difficulty=%s source=%s",
            topic,
            int(planned_question.get("difficulty", difficulty)),
            (planned_metadata or {}).get("source", "unknown"),
        )
        return planned_question

    raise ValueError(f"Diagnostic generation failed at difficulty {difficulty}.")


@router.post("/diagnostic/generate")
async def diagnostic_generate(req: DiagnosticGenerateRequest):
    """
    Generate the next diagnostic question using a two-stage plan:
      1. coverage
      2. uncertainty refinement
    """
    topics = req.topics or ([req.topic] if req.topic else [])
    if not topics:
        raise HTTPException(status_code=422, detail="No topics provided for diagnostic generation.")

    results_so_far = [r.model_dump() for r in req.results_so_far]

    try:
        plan = plan_diagnostic_question(
            topics=topics,
            results_so_far=results_so_far,
            question_index=req.question_index,
            target_questions=req.target_questions,
        )

        question = await _generate_diagnostic_question_with_fallback(
            topic=plan["topic"],
            difficulty=plan["difficulty"],
            source_text=req.source_text,
        )
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {
        "success": True,
        "question": question,
        "question_index": req.question_index,
        "topic": plan["topic"],
        "difficulty": int(question.get("difficulty", plan["difficulty"])),
        "total_questions": plan["target_questions"],
        "coverage_goal": plan["coverage_goal"],
        "phase": plan["phase"],
        "lecture_baseline_preview": plan["lecture_baseline_preview"],
    }


@router.post("/diagnostic/complete")
async def diagnostic_complete(req: DiagnosticCompleteRequest):
    if not req.topics:
        raise HTTPException(status_code=422, detail="No topics provided.")

    state = await _get_state(req.student_id, req.course_id, req.topics)

    results_as_dicts = [r.model_dump() for r in req.results]
    now_iso = datetime.now(timezone.utc).isoformat()

    state = apply_diagnostic_results(
        state=state,
        topics=req.topics,
        results=results_as_dicts,
        content_id=req.content_id,
        source_version=req.source_version,
    )

    content_key = make_content_key(req.content_id, req.source_version)
    state["diagnostic_status_by_content"][content_key]["timestamp"] = now_iso

    await _save_state(state)

    status = state["diagnostic_status_by_content"][content_key]

    return {
        "success": True,
        "content_id": req.content_id,
        "content_key": content_key,
        "lecture_baseline": status["mastery"],
        "lecture_label": _mastery_label(status["mastery"]),
        "topic_masteries": status["topic_masteries"],
        "topic_evidence_counts": status["topic_evidence_counts"],
        "topic_uncertainty": status["topic_uncertainty"],
        "topic_provisional_bands": status["topic_provisional_bands"],
        "topics_calibrated": req.topics,
        "correct_answers": sum(1 for r in req.results if r.correct),
        "total_questions": len(req.results),
        "expected_questions": status["expected_questions"],
        "coverage_goal": status["coverage_goal"],
    }


@router.post("/session/finalize")
async def session_finalize(req: SessionFinalizeRequest):
    """
    Called after ALL diagnostics for a session are complete.

    This is the only place that:
      - creates the student_session_history document
      - increments session_count
      - fires background cache fill
      - returns the first real quiz question
    """
    db = get_db()

    requested_mode = (
        req.mode
        if req.mode in {"auto", "normal_practice", "weakness_review", "challenge"}
        else "normal_practice"
    )
    requested_session_origin = _normalize_session_origin(req.session_origin)

    # Re-resolve selected content by real _id
    resolved_topics: list[str] = []
    resolved_source_text: str = ""
    resolved_items: list[dict] = []
    resolved_content_titles: list[str] = []

    for content_id in req.content_ids:
        try:
            oid = ObjectId(content_id)
        except Exception:
            continue

        item = await db.course_content.find_one({
            "_id": oid,
            "course_id": req.course_id,
            "active": True,
        })
        if not item:
            continue

        resolved_topics.extend(item.get("topics", []))
        resolved_source_text += "\n\n" + item.get("source_text", "")
        resolved_content_titles.append(item.get("title", ""))

        resolved_items.append({
            "content_id": str(item["_id"]),
            "source_version": (
                item.get("updated_at")
                or item.get("uploaded_at")
                or str(item["_id"])
            ),
        })

    # Deduplicate topics while preserving order
    seen = set()
    deduped_topics = []
    for topic in resolved_topics:
        if topic not in seen:
            seen.add(topic)
            deduped_topics.append(topic)
    resolved_topics = deduped_topics
    resolved_content_titles = _dedupe_keep_order(resolved_content_titles)
    source_scope_key = _make_source_scope_key(resolved_source_text)

    if not resolved_topics:
        raise HTTPException(status_code=404, detail="No active content found.")

    scoped_topics = list(resolved_topics)
    resolved_topics, applied_focus_topics, focused_topic = _apply_focus_topics(resolved_topics, req.focus_topics)
    if applied_focus_topics:
        logger.info(
            "[SESSION] Focus topics applied topics=%s mode=%s",
            applied_focus_topics,
            requested_mode,
        )
    elif req.focus_topics:
        logger.info(
            "[SESSION] Focus topic unavailable requested=%s mode=%s",
            req.focus_topics,
            requested_mode,
        )

    state = await _get_state(req.student_id, req.course_id, resolved_topics)

    if "topic_mastery_source" not in state:
        state["topic_mastery_source"] = {}

    for topic in resolved_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5
        if topic not in state["topic_mastery_source"]:
            state["topic_mastery_source"][topic] = "default_prior"

    for topic in scoped_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5
        if topic not in state["topic_mastery_source"]:
            state["topic_mastery_source"][topic] = "default_prior"

    if requested_mode == "challenge":
        challenge_readiness = _build_challenge_readiness(
            topic_mastery=state.get("topic_mastery", {}),
            scoped_topics=scoped_topics,
        )
        if not challenge_readiness["ready"]:
            await _save_state(state)
            logger.info(
                "[CHALLENGE] Blocked finalize course=%s topics=%s avg=%.3f proficient=%s/%s",
                req.course_id,
                len(scoped_topics),
                challenge_readiness["avg_mastery"],
                challenge_readiness["proficient_topic_count"],
                challenge_readiness["required_proficient_topics"],
            )
            return _challenge_not_ready_response(challenge_readiness)

    # Enforce that every selected content item is diagnosed at its current version
    for item in resolved_items:
        content_key = make_content_key(item["content_id"], item["source_version"])
        if not is_content_diagnosed(state, content_key):
            raise HTTPException(
                status_code=422,
                detail="Diagnostic incomplete for one or more selected lectures."
            )

    # Pick mode-aware starting topic + difficulty
    scoped_mastery = {
        topic: state["topic_mastery"].get(topic, 0.5)
        for topic in resolved_topics
    }

    start_topic, effective_mode = select_next_topic(
        scoped_mastery,
        mode=requested_mode,
        recent_answers=state.get("recent_answers", []),
        total_answers=state.get("total_answers", 0),
    )

    resolved_topics = [start_topic] + [t for t in resolved_topics if t != start_topic]

    state["session_topics"] = resolved_topics
    state["current_session_mode"] = requested_mode
    _clear_recovery_state(state)
    is_focused_followup = _set_session_followup_state(
        state=state,
        mode=requested_mode,
        focus_topics=applied_focus_topics,
        focused_topic=focused_topic,
        session_origin=requested_session_origin,
    )
    state["current_source_scope_key"] = source_scope_key
    state["current_cache_prompt_version"] = CACHE_PROMPT_VERSION
    state["current_difficulty"] = select_session_start_difficulty(
        state.get("topic_mastery", {}),
        resolved_topics,
        mode=requested_mode,
    )

    mastery_before = {
        t: state["topic_mastery"].get(t, 0.5)
        for t in resolved_topics
    }

    session_id = await _create_session_history(
        student_id=req.student_id,
        course_id=req.course_id,
        session_topics=resolved_topics,
        mastery_before=mastery_before,
        start_difficulty=state["current_difficulty"],
        target_questions=req.question_count,
        selected_content_ids=req.content_ids,
        selected_content_titles=resolved_content_titles,
        selected_mode=requested_mode,
        session_origin=requested_session_origin,
    )

    state["session_count"] = state.get("session_count", 0) + 1
    await _save_state(state)

    if is_focused_followup:
        cache_target = _cache_target_for_state(
            state,
            resolved_topics[0],
            state["current_difficulty"],
        )
        logger.info(
            "[FOLLOWUP] Eager prefill started topic=%s difficulty=%s",
            resolved_topics[0],
            state["current_difficulty"],
        )
        asyncio.create_task(
            _replenish_cache(
                topic=resolved_topics[0],
                difficulty=state["current_difficulty"],
                course_id=req.course_id,
                source_text=resolved_source_text,
                source_scope_key=source_scope_key,
                prompt_version=CACHE_PROMPT_VERSION,
                target=cache_target,
            )
        )

    first_topic = resolved_topics[0]

    gen_resp = await _generate_first_question(
        state=state,
        student_id=req.student_id,
        course_id=req.course_id,
        topic=first_topic,
        difficulty=state["current_difficulty"],
        source_text=resolved_source_text,
        session_id=session_id,
        max_questions=req.question_count,
    )

    return {
        "success": True,
        "session_id": session_id,
        "topics": resolved_topics,
        "resolved_source_text": resolved_source_text,
        "current_difficulty": state["current_difficulty"],
        "topic_mastery": state["topic_mastery"],
        "irt_active": state["irt_active"],
        "first_question": gen_resp,
        "selected_mode": requested_mode,
        "session_origin": requested_session_origin,
        "effective_mode": effective_mode,
    }

@router.post("/support/recovery/start")
async def support_recovery_start(req: RecoveryStartRequest):
    if not req.session_id:
        raise HTTPException(status_code=400, detail="Recovery step requires an active session.")

    state = await _get_state(req.student_id, req.course_id, [req.topic])
    offer = state.get("pending_recovery_offer") or {}
    if (
        offer.get("session_id") != req.session_id
        or offer.get("topic") != req.topic
        or offer.get("status") != "offered"
    ):
        raise HTTPException(status_code=409, detail="Recovery step is not available for this question.")

    simpler_explanation = req.explanation
    try:
        simpler_explanation = await generate_simple_explanation(
            topic=req.topic,
            question=req.question_text,
            explanation=req.explanation,
        )
    except Exception:
        simpler_explanation = req.explanation + "\n\n(Tip: Try breaking this concept into smaller steps.)"

    recovery_difficulty = max(1, int(offer.get("difficulty", req.difficulty)) - 1)
    source_scope_key = state.get("current_source_scope_key") or _make_source_scope_key(req.source_text)
    prompt_version = state.get("current_cache_prompt_version") or CACHE_PROMPT_VERSION
    recent_hashes = _recent_seen_hashes_from_state(state, req.course_id)

    try:
        question = await _generate_question_with_soft_dedupe(
            topic=req.topic,
            difficulty=recovery_difficulty,
            source_text=req.source_text,
            course_id=req.course_id,
            recent_hashes=recent_hashes,
        )
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    generated_difficulty = min(
        int(question.get("difficulty", recovery_difficulty)),
        int(offer.get("difficulty", req.difficulty)),
    )
    question["difficulty"] = generated_difficulty
    question["is_recovery_step"] = True
    question["recovery_for_topic"] = req.topic
    question["recovery_trigger_reason"] = offer.get("trigger_reason")
    question["recovery_intro_title"] = "Guided recovery step"
    question["recovery_intro_text"] = simpler_explanation

    _record_seen_question(
        state=state,
        question=question,
        course_id=req.course_id,
        topic=req.topic,
        difficulty=generated_difficulty,
        source_scope_key=source_scope_key,
        prompt_version=prompt_version,
    )
    _activate_recovery_step(
        state,
        session_id=req.session_id,
        topic=req.topic,
        trigger_reason=offer.get("trigger_reason", RECOVERY_REASON_THOUGHTFUL),
        recovery_difficulty=generated_difficulty,
        recovery_question_id=str(question.get("question", ""))[:80],
    )
    await _save_state(state)

    logger.info(
        "[RECOVERY] Started session=%s topic=%s difficulty=%s reason=%s",
        req.session_id,
        req.topic,
        generated_difficulty,
        offer.get("trigger_reason"),
    )

    return {
        "success": True,
        "simpler_explanation": simpler_explanation,
        "question": question,
        "recovery_topic": req.topic,
        "recovery_reason": offer.get("trigger_reason"),
        "recovery_reason_label": _recovery_reason_label(offer.get("trigger_reason")),
    }


@router.post("/support/recovery/decline")
async def support_recovery_decline(req: RecoveryDeclineRequest):
    state = await _get_state(req.student_id, req.course_id, [req.topic])
    offer = state.get("pending_recovery_offer") or {}
    if offer.get("session_id") == req.session_id and offer.get("topic") == req.topic:
        _clear_recovery_state(state)
        await _save_state(state)
        logger.info("[RECOVERY] Declined session=%s topic=%s", req.session_id, req.topic)

    return {"success": True}


@router.post("/support/recovery/submit")
async def support_recovery_submit(req: RecoverySubmitRequest):
    if not req.session_id:
        raise HTTPException(status_code=400, detail="Recovery answer requires an active session.")

    recovery_topic = str(req.recovery_for_topic or req.topic).strip()
    state = await _get_state(req.student_id, req.course_id, [recovery_topic or req.topic])
    active_recovery = state.get("active_recovery_step") or {}
    if (
        active_recovery.get("session_id") != req.session_id
        or active_recovery.get("topic") != recovery_topic
    ):
        raise HTTPException(status_code=409, detail="No active recovery step is available.")

    is_correct = req.selected_answer == req.correct_answer
    normalized_confidence = normalize_confidence_level(req.confidence)
    question_entry = {
        "question_id": req.question_id,
        "question_text": req.question_text or req.question_id,
        "options": req.options or {},
        "explanation": req.explanation or "",
        "topic": req.topic,
        "difficulty": req.difficulty,
        "selected_answer": req.selected_answer,
        "correct_answer": req.correct_answer,
        "is_correct": is_correct,
        "time_spent_ms": req.time_spent_ms,
        "time_context": _normalize_recovery_time_context(req.time_context) or "unknown",
        "confidence": normalized_confidence,
        "support_features_shown": [],
        "is_recovery_step": True,
        "recovery_for_topic": recovery_topic,
        "recovery_trigger_reason": active_recovery.get("trigger_reason"),
        "recovery_outcome": "recovered" if is_correct else "still_needs_review",
        "counts_toward_session_score": False,
    }
    await _append_question_log(
        session_id=req.session_id,
        question_entry=question_entry,
        is_correct=is_correct,
        time_spent_ms=req.time_spent_ms,
        question_difficulty=req.difficulty,
        end_difficulty=state.get("current_difficulty", req.difficulty),
        counts_toward_session=False,
    )

    _clear_recovery_state(state)
    await _save_state(state)

    logger.info(
        "[RECOVERY] Completed session=%s topic=%s correct=%s",
        req.session_id,
        recovery_topic,
        is_correct,
    )

    return {
        "success": True,
        "is_correct": is_correct,
        "correct_answer": req.correct_answer,
        "explanation": req.explanation or "",
        "support_features": ["explain_simpler"],
        "session_complete": False,
        "recovery_step_result": True,
        "recovery_result_message": (
            "That recovery step showed the concept is starting to click. We'll return to the normal quiz now."
            if is_correct
            else "That recovery step still showed some difficulty. We'll return to the quiz and keep this topic in view."
        ),
        "recovery_topic": recovery_topic,
        "recovery_outcome": "recovered" if is_correct else "still_needs_review",
    }


@router.post("/support/explain")
async def support_explain(data: dict):
    """Generate a simpler explanation for a question the student got wrong."""
    topic       = data.get("topic", "this topic")
    question    = data.get("question", "")
    explanation = data.get("explanation", "")

    try:
        simpler = await generate_simple_explanation(
            topic=topic,
            question=question,
            explanation=explanation
        )
        return {"simpler_explanation": simpler}

    except Exception:
        return {
            "simpler_explanation": explanation + "\n\n(Tip: Try breaking this concept into smaller steps.)"
        }

@router.post("/support/similar")
async def support_similar(req: GenerateRequest):
    """Generate a similar question on the same topic — one more like this."""
    try:
        question = await generate_question(
            topic=req.topic,
            difficulty=req.difficulty,
            source_text=req.source_text
        )
        return question
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    
@router.post("/content/parse")
async def parse_content(data: dict):
    """
    Parse a PDF (base64) or pasted lecture text and extract structured metadata.

    Phase 1 rules:
    - lecture only
    - source_text comes from deterministic extraction, not the LLM
    - LLM only suggests title, week, topics, summary
    """
    pdf_base64 = data.get("pdf_base64", "")
    raw_text_input = data.get("raw_text", "")

    raw_text = ""
    sample_text = ""
    page_count = 0

    if pdf_base64:
        try:
            result = extract_text_from_pdf_base64(pdf_base64)
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e))

        if result["is_empty"]:
            raise HTTPException(
                status_code=422,
                detail=(
                    "This PDF appears to be image-based or scanned — no extractable text was found. "
                    "OCR is not supported yet. Please upload a text-based PDF or paste the lecture text directly."
                )
            )

        raw_text = result["raw_text"]
        sample_text = result["sample_text"]
        page_count = result["page_count"]

    elif raw_text_input:
        raw_text = raw_text_input.strip()
        sample_text = raw_text[:12000]
        page_count = 0

    if not raw_text or len(raw_text.strip()) < 50:
        raise HTTPException(
            status_code=422,
            detail="Not enough text content to work with. The PDF may be image-only or the pasted text is too short."
        )

    try:
        metadata = await extract_content_metadata(sample_text)
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))

    return {
        "success": True,
        "extracted": {
            "suggested_title": metadata.get("suggested_title", ""),
            "suggested_week": metadata.get("suggested_week", 1),
            "suggested_content_type": "lecture",
            "topics": metadata.get("topics", []),
            "summary": metadata.get("summary", ""),
            "source_text": raw_text,
        },
        "page_count": page_count,
        "char_count": len(raw_text),
        "sample_used": len(sample_text),
    }
    
@router.post("/content/add")
async def add_content(item: ContentItem):
    """Instructor adds a content item (paste text + metadata)."""
    db = get_db()
    doc = item.model_dump()
    doc["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    await db.course_content.insert_one(doc)
    return {"success": True, "message": f"Content '{item.title}' added."}

@router.post("/content/update")
async def update_content_item(req: ContentUpdateRequest):
    db = get_db()
    oid = _parse_object_id(req.content_id)

    result = await db.course_content.update_one(
        {"_id": oid},
        {
            "$set": {
    "course_id": req.course_id,
    "course_name": req.course_name,
    "week": req.week,
    "content_type": "lecture",
    "title": req.title,
    "topics": req.topics,
    "source_text": req.source_text,
    "active": req.active,
    "updated_at": datetime.now(timezone.utc).isoformat(),
}
        }
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Content item not found.")

    return {"success": True, "message": "Content item updated successfully."}

@router.post("/content/toggle")
async def toggle_content_item(req: ContentToggleRequest):
    db = get_db()
    oid = _parse_object_id(req.content_id)

    result = await db.course_content.update_one(
        {"_id": oid},
        {"$set": {"active": req.active}}
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Content item not found.")

    return {
        "success": True,
        "message": "Content item activated." if req.active else "Content item deactivated."
    }

@router.get("/content/item/{content_id}")
async def get_content_item(content_id: str):
    db = get_db()
    oid = _parse_object_id(content_id)

    doc = await db.course_content.find_one({"_id": oid})
    if not doc:
        raise HTTPException(status_code=404, detail="Content item not found.")

    return {
        "success": True,
        "item": _serialize_content_item(doc, include_source_text=True)
    }

@router.get("/content/{course_id}")
async def get_content(course_id: str, include_inactive: bool = False):
    db = get_db()

    query = {"course_id": course_id}
    if not include_inactive:
        query["active"] = True

    docs = await db.course_content.find(query).sort([
        ("week", 1),
        ("title", 1),
    ]).to_list(length=None)

    return {
        "course_id": course_id,
        "items": [_serialize_content_item(doc) for doc in docs]
    }

@router.get("/courses")
async def list_courses():
    """Return all available courses with friendly names if available."""
    db = get_db()

    docs = await db.course_content.find(
        {"active": True},
        {"_id": 0, "course_id": 1, "course_name": 1}
    ).to_list(500)

    seen = {}
    for doc in docs:
        cid = doc.get("course_id")
        if not cid:
            continue

        cname = doc.get("course_name") or cid
        if cid not in seen:
            seen[cid] = {
                "course_id": cid,
                "course_name": cname
            }

    courses = sorted(seen.values(), key=lambda x: x["course_id"])
    return {"courses": courses}

@router.get("/sessions/{student_id}/{course_id}")
async def get_session_history(
    student_id: str,
    course_id: str,
    limit: int = 5,
    include_questions: bool = False,
):
    """Return completed sessions for dashboard preview or full history view."""
    db = get_db()

    limit = max(1, min(limit, 100))

    cursor = db.student_session_history.find(
        {
            "student_id": student_id,
            "course_id": course_id,
            "ended_at": {"$ne": None},
        }
    ).sort("started_at", -1)

    docs = await cursor.to_list(limit)

    sessions = [
        _serialize_session(doc, include_questions=include_questions)
        for doc in docs
    ]
    return {"sessions": sessions}


@router.get("/session/{student_id}/{course_id}/{session_id}")
async def get_session_detail(
    student_id: str,
    course_id: str,
    session_id: str,
):
    db = get_db()
    doc = await db.student_session_history.find_one(
        {
            "student_id": student_id,
            "course_id": course_id,
            "session_id": session_id,
            "ended_at": {"$ne": None},
        }
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found.")

    return {"success": True, "session": _serialize_session(doc, include_questions=True)}


@router.get("/mistake-journal/{student_id}/{course_id}")
async def get_mistake_journal(student_id: str, course_id: str):
    groups = await _build_mistake_journal(student_id, course_id)
    return {
        "success": True,
        "course_id": course_id,
        "groups": _serialize_mistake_journal_summary(groups),
    }


@router.get("/mistake-journal/review/{student_id}/{course_id}")
async def get_mistake_journal_review(
    student_id: str,
    course_id: str,
    lecture_key: str,
    topic: str,
):
    groups = await _build_mistake_journal(student_id, course_id)

    matching_group = next(
        (group for group in groups if str(group.get("lecture_key") or "") == str(lecture_key or "")),
        None,
    )
    if not matching_group:
        raise HTTPException(status_code=404, detail="Mistake lecture group not found.")

    matching_topic = next(
        (
            topic_group for topic_group in (matching_group.get("topics") or [])
            if str(topic_group.get("topic") or "") == str(topic or "")
        ),
        None,
    )
    if not matching_topic:
        raise HTTPException(status_code=404, detail="Mistake topic group not found.")

    return {
        "success": True,
        "course_id": course_id,
        "lecture": {
            "lecture_key": matching_group.get("lecture_key"),
            "lecture_title": matching_group.get("lecture_title"),
            "lecture_scope_kind": matching_group.get("lecture_scope_kind"),
            "lecture_week": matching_group.get("lecture_week"),
        },
        "topic": matching_topic.get("topic"),
        "mistake_count": matching_topic.get("mistake_count", 0),
        "latest_at": matching_topic.get("latest_at"),
        "entries": matching_topic.get("entries", []),
    }
