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
            ],
        )

        # ───── INDEXES ─────
        await database["reward_codes"].create_index("code", unique=True)
        await database["webauthn_credentials"].create_index("credential_id", unique=True)
        await database["app_notifications"].create_index("created_at")
        await database["appeals"].create_index([("user_id", 1), ("status", 1)])

        logging.info("✅ MongoDB + Beanie initialized successfully")

    except Exception as e:
        logging.exception("❌ Database initialization failed")
        raise


async def close_db():
    """Proper cleanup for production / reload environments"""
    global client
    if client:
        client.close()
        logging.info("🧹 MongoDB connection closed")
