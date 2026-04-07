from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
from uuid import uuid4
from app.models.quiz import (
    GenerateRequest,
    SubmitRequest,
    SubmitResponse,
    MasteryResponse,
    ContentItem,
)
from app.services.ai_engine import generate_question, generate_simple_explanation
from app.services.adaptation import (
    get_initial_student_state, process_answer, select_next_topic
)
from app.db.mongodb import get_db
import asyncio

router = APIRouter(prefix="/api/quiz", tags=["quiz"])


def _state_key(student_id: str, course_id: str) -> dict:
    return {"student_id": student_id, "course_id": course_id}

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

    practiced_mastery_after = {
        topic: topic_mastery_after.get(topic, 0.5)
        for topic in practiced_topics
        if topic in topic_mastery_after
    }

    weakest_topic = None
    strongest_topic = None

    # Prefer practiced topics for learner-facing insight
    if practiced_mastery_after:
        weakest_topic = min(practiced_mastery_after, key=practiced_mastery_after.get)
        strongest_topic = max(practiced_mastery_after, key=practiced_mastery_after.get)
    elif topic_mastery_after:
        # fallback only if something goes wrong
        weakest_topic = min(topic_mastery_after, key=topic_mastery_after.get)
        strongest_topic = max(topic_mastery_after, key=topic_mastery_after.get)

    recommendation = None
    if weakest_topic:
        recommendation = (
            f"Among the topics you practiced, {weakest_topic} needs the most review. "
            f"Revisit it before your next session."
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
        "weakest_topic_this_session": weakest_topic,
        "strongest_topic_this_session": strongest_topic,
        "recommendation": recommendation,
    }


def _serialize_session(doc: dict) -> dict:
    return {
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
        "end_difficulty": doc.get("end_difficulty", 2),
    }

async def _get_cached_question(topic: str, difficulty: int, course_id: str) -> dict | None:
    db = get_db()
    question = await db.questions_cache.find_one({
        "topic": topic,
        "difficulty": difficulty,
        "course_id": course_id,
        "used": False
    })
    if question:
        await db.questions_cache.update_one(
            {"_id": question["_id"]},
            {"$set": {"used": True}}
        )
        question.pop("_id", None)
    return question


async def _replenish_cache(topic: str, difficulty: int,
                           course_id: str, source_text: str,
                           target: int = 5):
    db = get_db()
    count = await db.questions_cache.count_documents({
        "topic": topic,
        "difficulty": difficulty,
        "course_id": course_id,
        "used": False
    })
    needed = max(0, target - count)
    for _ in range(needed):
        try:
            await asyncio.sleep(2)
            q = await generate_question(topic, difficulty, source_text)
            q["course_id"] = course_id
            q["used"] = False
            q["generated_at"] = datetime.now(timezone.utc).isoformat()
            await db.questions_cache.insert_one(q)
        except Exception:
            break

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate(req: GenerateRequest):
    """Generate next question — serve from cache if available."""
    try:
        # Try cache first
        cached = await _get_cached_question(req.topic, req.difficulty, req.course_id)
        if cached:
            # Fire background replenishment
            asyncio.create_task(
                _replenish_cache(req.topic, req.difficulty,
                                 req.course_id, req.source_text)
            )
            return cached

        # Cache miss — live generation
        question = await generate_question(
            topic=req.topic,
            difficulty=req.difficulty,
            source_text=req.source_text
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
        time_ms=req.time_spent_ms
    )

    await _save_state(state)

    # Decide next parameters (scoped to current session topics)
    allowed_topics = state.get("session_topics") or list(state["topic_mastery"].keys()) or [req.topic]

    scoped_mastery = {
        topic: state["topic_mastery"].get(topic, 0.5)
        for topic in allowed_topics
    }

    next_topic, next_mode = select_next_topic(scoped_mastery)
    next_difficulty = state["current_difficulty"]
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

    if not is_correct:
        # Always show normal explanation and offer a simpler version immediately
        support_features.append("explanation")
        support_features.append("explain_simpler")

        # If the learner is struggling repeatedly, offer reinforcement
        if consecutive_wrong >= 2:
            support_features.append("one_more_like_this")

    session_complete = False
    session_summary = {}

    if req.session_id:
        question_entry = {
            "question_id": req.question_id,
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
    db = get_db()

    resolved_topics = []
    resolved_source_text = ""

    if req.content_ids:
        # Resolve content items from MongoDB
        for title in req.content_ids:
            item = await db.course_content.find_one(
                {"course_id": req.course_id, "title": title, "active": True},
                {"_id": 0}
            )
            if item:
                resolved_topics.extend(item.get("topics", [title]))
                resolved_source_text += "\n\n" + item.get("source_text", "")

        # Deduplicate topics while preserving order
        seen = set()
        deduped = []
        for t in resolved_topics:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        resolved_topics = deduped

        if not resolved_topics:
            raise HTTPException(
                status_code=404,
                detail="No active content found for the selected items."
            )
    else:
        resolved_topics = [t.strip() for t in req.topic.split(",") if t.strip()]
        resolved_source_text = req.source_text

    # Get or create student state
    state = await _get_state(req.student_id, req.course_id, resolved_topics)

    # Add any new topics not yet in student state
    for topic in resolved_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5

    # Restrict this session to the learner-selected topics only
    state["session_topics"] = resolved_topics

    # Increment session count
    state["session_count"] = state.get("session_count", 0) + 1
    await _save_state(state)

    mastery_before = {
        topic: state["topic_mastery"].get(topic, 0.5)
        for topic in resolved_topics
    }

    session_id = await _create_session_history(
        student_id=req.student_id,
        course_id=req.course_id,
        session_topics=resolved_topics,
        mastery_before=mastery_before,
        start_difficulty=state["current_difficulty"],
        target_questions=req.question_count,
        selected_content_ids=req.content_ids or [],
    )

    # Fire background cache fill for all resolved topics
    for topic in resolved_topics:
        asyncio.create_task(
            _replenish_cache(
                topic,
                state["current_difficulty"],
                req.course_id,
                resolved_source_text,
                target=3
            )
        )

    return {
        "student_id": req.student_id,
        "course_id": req.course_id,
        "session_id": session_id,
        "topics": resolved_topics,
        "resolved_source_text": resolved_source_text,
        "session_count": state["session_count"],
        "current_difficulty": state["current_difficulty"],
        "topic_mastery": state["topic_mastery"],
        "irt_active": state["irt_active"],
        "cache_filling": True,
        "message": f"Session started. Pre-generating questions for {len(resolved_topics)} topic(s)."
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
    
@router.post("/content/add")
async def add_content(item: ContentItem):
    """Instructor adds a content item (paste text + metadata)."""
    db = get_db()
    doc = item.model_dump()
    doc["uploaded_at"] = datetime.now(timezone.utc).isoformat()
    await db.course_content.insert_one(doc)
    return {"success": True, "message": f"Content '{item.title}' added."}

@router.get("/content/{course_id}")
async def list_content(course_id: str):
    """Student fetches available content items for this course."""
    db = get_db()
    items = await db.course_content.find(
        {"course_id": course_id, "active": True},
        {"_id": 0}
    ).to_list(100)
    return {"course_id": course_id, "items": items}

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
async def get_session_history(student_id: str, course_id: str, limit: int = 5):
    """Return recent completed sessions for dashboard/history view."""
    db = get_db()
    docs = await db.student_session_history.find(
        {
            "student_id": student_id,
            "course_id": course_id,
            "ended_at": {"$ne": None},
        }
    ).sort("started_at", -1).to_list(limit)

    sessions = [_serialize_session(doc) for doc in docs]
    return {"sessions": sessions}