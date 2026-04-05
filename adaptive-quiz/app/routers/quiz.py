from fastapi import APIRouter, HTTPException
from datetime import datetime, timezone
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
    """Submit an answer, update mastery, return next question parameters."""
    is_correct = req.selected_answer == req.correct_answer

    # Load state from MongoDB
    state = await _get_state(req.student_id, req.course_id, [req.topic])

    # Add any new topic that wasn't in the original state
    if req.topic not in state["topic_mastery"]:
        state["topic_mastery"][req.topic] = 0.5

    # Run adaptive logic
    state = process_answer(
        state=state,
        topic=req.topic,
        correct=is_correct,
        time_ms=req.time_spent_ms
    )

    # Save updated state to MongoDB
    await _save_state(state)

    # Decide next question parameters
    next_topic, next_mode   = select_next_topic(state["topic_mastery"])
    next_difficulty         = state["current_difficulty"]
    updated_mastery         = state["topic_mastery"].get(req.topic, 0.5)

    # Support features
    support_features = []
    recent = state["recent_answers"]
    if not is_correct:
        support_features.append("explanation")
    consecutive_wrong = 0
    for ans in reversed(recent):
        if not ans["correct"]:
            consecutive_wrong += 1
        else:
            break
    if consecutive_wrong >= 2:
        support_features.append("explain_simpler")
    if consecutive_wrong >= 3:
        support_features.append("one_more_like_this")

    return SubmitResponse(
        is_correct=is_correct,
        explanation="See the question explanation field.",
        updated_mastery=updated_mastery,
        next_difficulty=next_difficulty,
        next_topic=next_topic,
        next_mode=next_mode,
        support_features=support_features,
        session_complete=False
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

    mastery       = state["topic_mastery"]
    weak_topics   = [t for t, m in mastery.items() if m < 0.4]
    strong_topics = [t for t, m in mastery.items() if m >= 0.7]

    return MasteryResponse(
        student_id=student_id,
        course_id=course_id,
        topic_mastery=mastery,
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
        # Fallback — use topic/source_text from request directly
        resolved_topics = [t.strip() for t in req.topic.split(",") if t.strip()]
        resolved_source_text = req.source_text

    # Get or create student state
    state = await _get_state(req.student_id, req.course_id, resolved_topics)

    # Add any new topics not yet in student state
    for topic in resolved_topics:
        if topic not in state["topic_mastery"]:
            state["topic_mastery"][topic] = 0.5

    # Increment session count
    state["session_count"] = state.get("session_count", 0) + 1
    await _save_state(state)

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
    """Return all available course IDs that have active content."""
    db = get_db()
    courses = await db.course_content.distinct("course_id", {"active": True})
    return {"courses": sorted(courses)}