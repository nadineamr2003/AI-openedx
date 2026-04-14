from motor.motor_asyncio import AsyncIOMotorClient
from app.config import MONGODB_URI
from pymongo import ASCENDING
import logging

logger = logging.getLogger(__name__)
client: AsyncIOMotorClient = None
db = None
_questions_cache_ttl_index_ensured = False


async def ensure_questions_cache_ttl_index():
    global _questions_cache_ttl_index_ensured

    if _questions_cache_ttl_index_ensured or db is None:
        return

    await db.questions_cache.create_index(
        [("expires_at", ASCENDING)],
        name="questions_cache_expires_at_ttl",
        expireAfterSeconds=0,
    )
    _questions_cache_ttl_index_ensured = True
    logger.info("[CACHE] TTL index ensured")

async def connect_db():
    global client, db
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client.adaptive_quiz
    await ensure_questions_cache_ttl_index()
    print("✅ Connected to MongoDB")

async def close_db():
    global client
    if client:
        client.close()
        print("🔌 MongoDB connection closed")

def get_db():
    return db
