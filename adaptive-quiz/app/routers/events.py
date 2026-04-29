from fastapi import APIRouter
from datetime import datetime, timezone
from app.db.mongodb import get_db

router = APIRouter(prefix="/api/events", tags=["events"])

@router.post("/track")
async def track_event(data: dict):
    db = get_db()
    doc = {
        "event_type": data.get("event_type"),
        "source": data.get("source", "unknown"),
        "student_id": data.get("student_id") or data.get("user_id"),
        "course_id": data.get("course_id"),
        "session_id": data.get("session_id"),
        "data": data.get("data", {}),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.behavioral_events.insert_one(doc)
    return {"success": True, "received": doc["event_type"]}

@router.get("/health")
async def events_health():
    return {"status": "ok", "service": "adaptive-quiz-events"}