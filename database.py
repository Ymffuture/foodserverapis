# backend/database.py
from motor.motor_asyncio import AsyncIOMotorClient
from beanie import init_beanie
from config import DATABASE_URL
import logging

# IMPORT REAL MODELS (NOT STRINGS)
from models.user import User
from models.menu import MenuItem
from models.order import Order
from models.suggestion import Suggestion
from models.delivery_driver import DeliveryDriver
from models.wallet_transaction import WalletTransaction
from models.delivery_assignment import DeliveryAssignment
from models.reward_code import RewardCode
from models.webauthn_credential import WebAuthnCredential
from models.notification import AppNotification
from models.saved_address import SavedAddress
from models.push_subscription import PushSubscription

from routes.appeals import AppealDoc
from models.social_interaction import SocialInteraction

client: AsyncIOMotorClient | None = None


async def init_db():
    global client

    try:
        client = AsyncIOMotorClient(DATABASE_URL)
        database = client["kotabites"]

        await init_beanie(
            database=database,
            document_models=[
                User,
                MenuItem,
                Order,
                Suggestion,
                DeliveryDriver,
                WalletTransaction,
                DeliveryAssignment,
                RewardCode,
                WebAuthnCredential,
                AppNotification,
                AppealDoc,
                SocialInteraction,
                SavedAddress,
                PushSubscription,
            ],
        )

        # ───── INDEXES ─────

        # reward_codes.code — must be UNIQUE.
        # Drop any existing non-unique "code_1" first so a re-deploy
        # on a live DB doesn't hit IndexKeySpecsConflict (error 86).
        try:
            await database["reward_codes"].drop_index("code_1")
            logging.info("🗑️  Dropped stale non-unique index 'code_1' on reward_codes")
        except Exception:
            pass  # Index didn't exist — that's fine
        await database["reward_codes"].create_index("code", unique=True)

        await database["webauthn_credentials"].create_index("credential_id", unique=True)
        await database["app_notifications"].create_index("created_at")
        await database["appeals"].create_index([("user_id", 1), ("status", 1)])

        logging.info("✅ MongoDB + Beanie initialized successfully")

    except Exception as e:
        logging.exception("❌ Database initialization failed")
        raise


async def close_db():
    global client
    if client:
        client.close()
        logging.info("🧹 MongoDB connection closed")
