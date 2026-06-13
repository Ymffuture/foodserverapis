# database.py
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
            "models.delivery_driver.DeliveryDriver",
            "models.wallet_transaction.WalletTransaction",
            "models.delivery_assignment.DeliveryAssignment",
            "models.reward_code.RewardCode",
            "models.webauthn_credential.WebAuthnCredential",
            "models.notification.AppNotification",
            "routes.appeals.AppealDoc",                            # ← NEW
        ]
    )

    try:
        await database["reward_codes"].create_index("code", unique=True)
        await database["webauthn_credentials"].create_index("credential_id", unique=True)
        await database["app_notifications"].create_index("created_at")
        await database["appeals"].create_index([("user_id", 1), ("status", 1)])  # ← NEW
    except Exception:
        pass

    print("✅ Connected to MongoDB + Beanie initialized (with appeals model)")
