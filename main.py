# backend/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from database import init_db

# ─────────────────────────────────────────────
# ROUTERS
# ─────────────────────────────────────────────

from routes.auth import router as auth_router
from routes.menu import router as menu_router
from routes.orders import router as orders_router
from routes.payments import router as payments_router
from routes.ai import router as ai_router
from routes.delivery import router as delivery_router
from routes.rewards import router as rewards_router
from routes.webauthn import router as webauthn_router
from routes.analytics import router as analytics_router
from routes.admin_users import router as admin_users_router
from routes.notifications import router as notifications_router
from routes.Users import router as users_router
from routes.appeals import router as appeals_router
from routes.social import router as social_router
from routes.reasoning import router as reasoning_router


# ─────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


# ─────────────────────────────────────────────
# APP INIT
# ─────────────────────────────────────────────

app = FastAPI(
    title="KotaBites API",
    description="Online ordering system with social + wallet + moderation",
    version="2.3.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────
# CORS (tighten in production)
# ─────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "https://your-domain.com"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# ROUTES (VERSIONED)
# ─────────────────────────────────────────────

API_PREFIX = "/api/v1"

app.include_router(auth_router, prefix=f"{API_PREFIX}/auth", tags=["Auth"])
app.include_router(menu_router, prefix=f"{API_PREFIX}/menu", tags=["Menu"])
app.include_router(orders_router, prefix=f"{API_PREFIX}/orders", tags=["Orders"])
app.include_router(payments_router, prefix=f"{API_PREFIX}/payments", tags=["Payments"])
app.include_router(ai_router, prefix=f"{API_PREFIX}/ai", tags=["AI"])
app.include_router(delivery_router, prefix=f"{API_PREFIX}/delivery", tags=["Delivery"])
app.include_router(rewards_router, prefix=f"{API_PREFIX}/rewards", tags=["Rewards"])
app.include_router(webauthn_router, prefix=f"{API_PREFIX}/webauthn", tags=["WebAuthn"])
app.include_router(analytics_router, prefix=f"{API_PREFIX}/analytics", tags=["Analytics"])
app.include_router(admin_users_router, prefix=f"{API_PREFIX}/admin", tags=["Admin"])
app.include_router(notifications_router, prefix=f"{API_PREFIX}/notifications", tags=["Notifications"])
app.include_router(users_router, prefix=f"{API_PREFIX}/users", tags=["Users"])
app.include_router(appeals_router, prefix=f"{API_PREFIX}/appeals", tags=["Appeals"])
app.include_router(social_router, prefix=f"{API_PREFIX}/social", tags=["Social"])
app.include_router(reasoning_router, prefix=f"{API_PREFIX}/reasoning", tags=["Reasoning"])


# ─────────────────────────────────────────────
# ROOT
# ─────────────────────────────────────────────

@app.get("/")
def home():
    return {
        "message": "KotaBites API v2.3 🔥",
        "status": "running",
        "features": [
            "Authentication (JWT + OAuth)",
            "Menu & Orders system",
            "AI chatbot + reasoning engine",
            "Delivery tracking system",
            "Wallet + rewards system",
            "WebAuthn login support",
            "Admin moderation tools",
            "Real-time notifications",
            "Social engagement system",
            "User appeals system",
        ],
    }
