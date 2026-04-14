from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
import json
import logging
from bson import ObjectId
from app.models.quiz import (
    GenerateRequest, SubmitRequest, SubmitResponse, MasteryResponse,
    ContentItem, ContentListResponse, ContentUpdateRequest, ContentToggleRequest,
    DiagnosticGenerateRequest, DiagnosticCompleteRequest,
    DiagnosticItem, SessionFinalizeRequest,
)
from app.services.adaptation import (
    get_initial_student_state,
    process_answer,
    select_next_topic,
    select_difficulty,
    make_content_key,
    is_content_diagnosed,
    apply_diagnostic_results,
    diagnostic_target_question_count,
    diagnostic_coverage_goal,
    plan_diagnostic_question,
    select_session_start_difficulty,
)
from app.services.ai_engine import (
    generate_question,
    generate_simple_explanation,
    extract_content_metadata,
)
from app.services.pdf_parser import extract_text_from_pdf_base64
from app.db.mongodb import get_db
import asyncio

router = APIRouter(prefix="/api/quiz", tags=["quiz"])
CACHE_PROMPT_VERSION = "quiz-cache-v2"
logger = logging.getLogger(__name__)
_REPLENISH_IN_FLIGHT: set[str] = set()


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
        "recommendation": None,
        "selected_mode": selected_mode,
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
):
    db = get_db()

    await db.student_session_history.update_one(
        {"session_id": session_id},
        {
            "$push": {
                "question_log": question_entry,
                "difficulty_path": question_difficulty,
            },
            "$inc": {
                "questions_answered": 1,
                "correct_answers": 1 if is_correct else 0,
                "total_time_spent_ms": time_spent_ms,
            },
            "$set": {
                "end_difficulty": end_difficulty,
            }
        }
    )
    
def _compute_topic_session_stats(question_log: list[dict]) -> dict[str, dict]:
    stats: dict[str, dict] = {}

    for entry in question_log:
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

def _build_session_recommendation(
    accuracy: float,
    review_topic: str | None,
    strongest_topic: str | None,
) -> str | None:
    if review_topic:
        if accuracy >= 0.8:
            if strongest_topic and strongest_topic != review_topic:
                return (
                    f"Strong session overall. You performed best on {strongest_topic}. "
                    f"The main topic still worth tightening up is {review_topic}."
                )
            return (
                f"Strong session overall. The only topic that still looks worth revisiting is {review_topic}."
            )

        if accuracy >= 0.60:
            return (
                f"Good progress. {review_topic} showed the most difficulty in this session, "
                f"so a short focused follow-up on it would help."
            )

        return (
            f"This session was challenging, which is completely normal. "
            f"Start your next attempt by reviewing {review_topic} step by step."
        )

    if strongest_topic:
        if accuracy >= 0.8:
            return (
                f"Excellent session. You handled all practiced topics well, with especially strong performance on {strongest_topic}. "
                f"You are ready to continue or try a more challenging follow-up."
            )
        return (
            f"Solid session. No single topic clearly needs review right now. "
            f"Keep practising to consolidate what you built here."
        )

    return None


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

    # NEW: derive practiced topics from the actual question log
    question_log = doc.get("question_log", [])
    practiced_topics = []
    seen = set()

    for entry in question_log:
        topic = entry.get("topic")
        if topic and topic not in seen:
            seen.add(topic)
            practiced_topics.append(topic)

    lectures_practised_count = len(doc.get("selected_content_ids", []) or [])
    topics_practised_count = len(practiced_topics)
    content_mastery_summaries = await _build_content_mastery_summaries(doc, topic_mastery_after)

    topic_session_stats = _compute_topic_session_stats(question_log)

    weakest_topic = None
    strongest_topic = None

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

        review_candidates = [
            t for t, s in topic_session_stats.items()
            if (
                (s["attempts"] >= 2 and s["accuracy"] < 0.6) or
                (s["wrong"] >= 2) or
                (s["attempts"] == 1 and s["accuracy"] == 0.0)
            )
        ]

        if review_candidates:
            weakest_topic = min(
                review_candidates,
                key=lambda t: (
                    topic_session_stats[t]["accuracy"],
                    -topic_session_stats[t]["wrong"],
                    -topic_session_stats[t]["attempts"],
                )
            )

    recommendation = _build_session_recommendation(
        accuracy=accuracy,
        review_topic=weakest_topic,
        strongest_topic=strongest_topic,
    )

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
                "weakest_topic_this_session": weakest_topic,
                "strongest_topic_this_session": strongest_topic,
                "recommendation": recommendation,
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
        "weakest_topic_this_session": weakest_topic,
        "strongest_topic_this_session": strongest_topic,
        "recommendation": recommendation,
    }


def _serialize_session(doc: dict, include_questions: bool = False) -> dict:
    item = {
        "session_id": doc.get("session_id"),
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
        "recommendation": doc.get("recommendation"),
        "end_difficulty": doc.get("end_difficulty", 3),
        "practiced_topics": doc.get("practiced_topics", []),
        "lectures_practised_count": doc.get("lectures_practised_count"),
        "topics_practised_count": doc.get("topics_practised_count"),
        "content_mastery_summaries": doc.get("content_mastery_summaries", []),
        "selected_mode": doc.get("selected_mode", "normal_practice"),
        "selected_content_titles": doc.get("selected_content_titles", []),
    }

    if include_questions:
        item["question_log"] = doc.get("question_log", [])

    return item

async def _get_cached_question(
    topic: str,
    difficulty: int,
    course_id: str,
    source_scope_key: str,
    prompt_version: str,
) -> dict | None:
    db = get_db()
    used_at = datetime.now(timezone.utc).isoformat()
    question = await db.questions_cache.find_one_and_update(
        {
            "topic": topic,
            "difficulty": difficulty,
            "course_id": course_id,
            "source_scope_key": source_scope_key,
            "prompt_version": prompt_version,
            "used": False,
        },
        {"$set": {"used": True, "used_at": used_at}},
        sort=[("generated_at", -1)],
    )
    if question:
        question["used"] = True
        question["used_at"] = used_at
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
    target: int = 1,
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
                generated_at = datetime.now(timezone.utc).isoformat()
                q["course_id"] = course_id
                q["used"] = False
                q["generated_at"] = generated_at
                q["used_at"] = None
                q["source_scope_key"] = source_scope_key
                q["prompt_version"] = prompt_version
                q["question_hash"] = _make_question_hash(q)

                result = await db.questions_cache.insert_one(q)

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
    cached = await _get_cached_question(
        topic=topic,
        difficulty=difficulty,
        course_id=course_id,
        source_scope_key=source_scope_key,
        prompt_version=CACHE_PROMPT_VERSION,
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
                target=1,
            )
        )
        question = cached
    else:
        logger.info(
            "[CACHE] Miss topic=%s requested_difficulty=%s prompt_version=%s",
            topic, difficulty, CACHE_PROMPT_VERSION
        )
        try:
            question = await generate_question(
                topic=topic, difficulty=difficulty, source_text=source_text
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
                    target=1,
                )
            )
        except ValueError as e:
            return {"success": False, "error": str(e)}

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

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate(req: GenerateRequest):
    """Generate next question — serve from cache if available."""
    try:
        state = await _get_state(req.student_id, req.course_id, [req.topic])
        source_scope_key = state.get("current_source_scope_key") or _make_source_scope_key(req.source_text)
        prompt_version = state.get("current_cache_prompt_version") or CACHE_PROMPT_VERSION

        # Try cache first
        cached = await _get_cached_question(
            topic=req.topic,
            difficulty=req.difficulty,
            course_id=req.course_id,
            source_scope_key=source_scope_key,
            prompt_version=prompt_version,
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
                    target=1,
                )
            )
            return cached

        # Cache miss — live generation
        logger.info(
            "[CACHE] Miss topic=%s requested_difficulty=%s prompt_version=%s",
            req.topic, req.difficulty, prompt_version
        )
        question = await generate_question(
            topic=req.topic,
            difficulty=req.difficulty,
            source_text=req.source_text
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
                target=1,
            )
        )
        return question

    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/submit", response_model=SubmitResponse)
async def submit(req: SubmitRequest):
    """Submit an answer, update mastery, log session analytics, return next parameters."""
    is_correct = req.selected_answer == req.correct_answer

    # Load state from MongoDB
    state = await _get_state(req.student_id, req.course_id, [req.topic])

    if req.topic not in state["topic_mastery"]:
        state["topic_mastery"][req.topic] = 0.5

    # Run adaptive logic
    state = process_answer(
        state=state,
        topic=req.topic,
        correct=is_correct,
        time_ms=req.time_spent_ms,
        time_context=req.time_context,
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

    await _save_state(state)

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

    if req.session_id:
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
        "support_features_shown": support_features,
        }

        await _append_question_log(
            session_id=req.session_id,
            question_entry=question_entry,
            is_correct=is_correct,
            time_spent_ms=req.time_spent_ms,
            question_difficulty=req.difficulty,
            end_difficulty=next_difficulty,
        )

        db = get_db()
        session_doc = await db.student_session_history.find_one({"session_id": req.session_id})
        if session_doc:
            answered = session_doc.get("questions_answered", 0)
            target = session_doc.get("target_questions", 10)
            session_complete = answered >= target

            if session_complete:
                topic_mastery_after = {
                    topic: state["topic_mastery"].get(topic, 0.5)
                    for topic in allowed_topics
                }
                session_summary = await _finalize_session_history(req.session_id, topic_mastery_after)

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
        weakest_topic_this_session=session_summary.get("weakest_topic_this_session"),
        strongest_topic_this_session=session_summary.get("strongest_topic_this_session"),
        session_accuracy=session_summary.get("accuracy"),
        avg_time_spent_ms=session_summary.get("avg_time_spent_ms"),
        lectures_practised_count=session_summary.get("lectures_practised_count"),
        topics_practised_count=session_summary.get("topics_practised_count"),
        content_mastery_summaries=session_summary.get("content_mastery_summaries"),
        narrative_bridge=narrative_bridge,
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

    # Load or create state
    state = await _get_state(req.student_id, req.course_id, resolved_topics)

    if "topic_mastery_source" not in state:
        state["topic_mastery_source"] = {}

    for topic in resolved_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5
        if topic not in state["topic_mastery_source"]:
            state["topic_mastery_source"][topic] = "default_prior"

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
    )

    state["session_count"] = state.get("session_count", 0) + 1
    await _save_state(state)

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
        "effective_mode": effective_mode,
    }
    
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

        question = await generate_question(
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
        "difficulty": plan["difficulty"],
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

    state = await _get_state(req.student_id, req.course_id, resolved_topics)

    if "topic_mastery_source" not in state:
        state["topic_mastery_source"] = {}

    for topic in resolved_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5
        if topic not in state["topic_mastery_source"]:
            state["topic_mastery_source"][topic] = "default_prior"

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
    )

    state["session_count"] = state.get("session_count", 0) + 1
    await _save_state(state)

    first_topic = resolved_topics[0]

    gen_resp = await _generate_first_question(
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
        "effective_mode": effective_mode,
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
