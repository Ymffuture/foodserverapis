# database.py
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from config import DATABASE_URL

client = None

async def init_db():
    global client
    client = AsyncIOMotorClient(DATABASE_URL)

    # Explicit DB name — works whether or not the URI includes one
    database = client["kotabites"]

    await init_beanie(
        database=database,
        document_models=[
            "models.user.User",
            "models.menu.MenuItem",
            "models.order.Order",
        ]
    )
    print("✅ Connected to MongoDB + Beanie initialized")
