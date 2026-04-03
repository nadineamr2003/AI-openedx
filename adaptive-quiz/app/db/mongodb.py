from motor.motor_asyncio import AsyncIOMotorClient
from app.config import MONGODB_URI

client: AsyncIOMotorClient = None
db = None

async def connect_db():
    global client, db
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client.adaptive_quiz
    print("✅ Connected to MongoDB")

async def close_db():
    global client
    if client:
        client.close()
        print("🔌 MongoDB connection closed")

def get_db():
    return db