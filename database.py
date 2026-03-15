# database.py  (updated — register Suggestion model)
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from config import DATABASE_URL

client = None

async def init_db():
    global client
    client = AsyncIOMotorClient(DATABASE_URL)

    database = client["kotabites"]

    await init_beanie(
        database=database,
        document_models=[
            "models.user.User",
            "models.menu.MenuItem",
            "models.order.Order",
            "models.suggestion.Suggestion",   # ← new
        ]
    )
    print("✅ Connected to MongoDB + Beanie initialized")
