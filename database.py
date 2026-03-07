# database.py
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from config import DATABASE_URL
import asyncio

client = None

async def init_db():
    global client
    client = AsyncIOMotorClient(DATABASE_URL)
    database = client.get_default_database()   # or client["kotabites"]

    await init_beanie(
        database=database,
        document_models=[
            "models.user.User",
            "models.menu.MenuItem",
            "models.order.Order",
        ]
    )
    print("✅ Connected to MongoDB + Beanie initialized")
