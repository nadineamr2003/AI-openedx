# app/services/analytics_logger.py

import logging
import httpx
from datetime import datetime, timezone
from app.config import settings

logger = logging.getLogger(__name__)

async def log_analytics_event(event_type: str, payload: dict):
    if not getattr(settings, "ANALYTICS_ENABLED", False):
        return

    endpoint = getattr(settings, "ANALYTICS_ENDPOINT_URL", None)
    if not endpoint:
        return

    event = {
        "event_type": event_type,
        "source": "adaptive_quiz",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": payload,
    }

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            await client.post(endpoint, json=event)
    except Exception as exc:
        logger.warning("[analytics] failed to send event %s: %s", event_type, exc)