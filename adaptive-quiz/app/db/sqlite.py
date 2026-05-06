"""SQLite connection helper for the analytics tracker.

The database is created on first run and lives at
   <project_root>/data/analytics.db
relative to this file (i.e. ~/Desktop/AI-OpenEdx/adaptive-quiz/data/analytics.db).
"""

from pathlib import Path
import aiosqlite

# data/ folder is created next to the app/ folder
DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "analytics.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT,
    session_id  TEXT,
    timestamp   TEXT,
    button      TEXT,
    action      TEXT  -- 'hovered' or 'clicked'
);
CREATE INDEX IF NOT EXISTS idx_events_username  ON events(username);
CREATE INDEX IF NOT EXISTS idx_events_action    ON events(action);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
"""


async def init_sqlite() -> None:
    """Create the events table + indexes if they don't already exist."""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
    print(f"✅ Analytics SQLite ready at {DB_PATH}")


def get_connection():
    """Open a new aiosqlite connection. Use as `async with get_connection() as conn:`."""
    return aiosqlite.connect(DB_PATH)
