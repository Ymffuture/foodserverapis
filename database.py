# database.py  (updated — register RewardCode model)
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
            "models.webauthn_credential.WebAuthnCredential", # ← NEW
        ]
    )

    # Ensure the reward_codes.code field has a unique index
    try:
        await database["reward_codes"].create_index("code", unique=True)
        await database["webauthn_credentials"].create_index("credential_id", unique=True)
    except Exception:
        pass  # index may already exist

    print("✅ Connected to MongoDB + Beanie initialized (with rewards model)")
