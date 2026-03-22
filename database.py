# database.py  (updated — register all delivery models)
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
            "models.suggestion.Suggestion",
            "models.delivery_driver.DeliveryDriver",         # ← new
            "models.wallet_transaction.WalletTransaction",   # ← new
            "models.delivery_assignment.DeliveryAssignment", # ← new
        ]
    )
    print("✅ Connected to MongoDB + Beanie initialized (with delivery models)")
