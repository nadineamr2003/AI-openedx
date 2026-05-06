"""Analytics router — ingests button click/hover events from the LMS tracker."""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.db.sqlite import get_connection

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class EventIn(BaseModel):
    username: str
    session_id: str
    timestamp: str
    button: str
    action: str = Field(pattern="^(hovered|clicked)$")


class EventBatch(BaseModel):
    events: List[EventIn]


@router.post("/events")
async def ingest_events(batch: EventBatch):
    if not batch.events:
        return {"inserted": 0}
    rows = [(e.username, e.session_id, e.timestamp, e.button, e.action) for e in batch.events]
    async with get_connection() as conn:
        await conn.executemany(
            "INSERT INTO events (username, session_id, timestamp, button, action) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await conn.commit()
    return {"inserted": len(rows)}


@router.get("/events")
async def list_events(
    username: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
):
    where, params = [], []
    if username:
        where.append("username = ?"); params.append(username)
    if action:
        where.append("action = ?"); params.append(action)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    sql = (
        "SELECT username, session_id, timestamp, button, action "
        f"FROM events {where_clause} "
        "ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    async with get_connection() as conn:
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()

    return {
        "events": [
            {"username": r[0], "session_id": r[1], "timestamp": r[2],
             "button": r[3], "action": r[4]}
            for r in rows
        ]
    }


@router.get("/summary")
async def summary():
    async with get_connection() as conn:
        async def one(sql, *args):
            cur = await conn.execute(sql, args)
            row = await cur.fetchone()
            return row[0] if row else 0

        total    = await one("SELECT COUNT(*) FROM events")
        clicks   = await one("SELECT COUNT(*) FROM events WHERE action='clicked'")
        hovers   = await one("SELECT COUNT(*) FROM events WHERE action='hovered'")
        users    = await one("SELECT COUNT(DISTINCT username) FROM events")
        sessions = await one("SELECT COUNT(DISTINCT session_id) FROM events")

        cur = await conn.execute(
            "SELECT button, COUNT(*) c FROM events WHERE action='clicked' "
            "GROUP BY button ORDER BY c DESC LIMIT 8"
        )
        top_clicked = [{"button": r[0], "count": r[1]} for r in await cur.fetchall()]

        cur = await conn.execute(
            "SELECT button, COUNT(*) c FROM events WHERE action='hovered' "
            "GROUP BY button ORDER BY c DESC LIMIT 8"
        )
        top_hovered = [{"button": r[0], "count": r[1]} for r in await cur.fetchall()]

    return {
        "total_events": total, "clicks": clicks, "hovers": hovers,
        "unique_users": users, "unique_sessions": sessions,
        "top_clicked": top_clicked, "top_hovered": top_hovered,
    }


@router.get("/timeline")
async def timeline(hours: int = 24):
    """Hourly bucket counts of clicks and hovers for the last N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with get_connection() as conn:
        cur = await conn.execute(
            """
            SELECT strftime('%Y-%m-%d %H:00', timestamp) AS hour,
                   action,
                   COUNT(*) AS c
            FROM events
            WHERE timestamp >= ?
            GROUP BY hour, action
            ORDER BY hour
            """,
            (cutoff,),
        )
        rows = await cur.fetchall()

    buckets = {}
    for hour, action, count in rows:
        if hour not in buckets:
            buckets[hour] = {"clicked": 0, "hovered": 0}
        buckets[hour][action] = count

    hours_list = sorted(buckets.keys())
    return {
        "hours":  hours_list,
        "clicks": [buckets[h].get("clicked", 0) for h in hours_list],
        "hovers": [buckets[h].get("hovered", 0) for h in hours_list],
    }
