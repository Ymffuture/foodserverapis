# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from database import init_db
from routes import auth, menu, orders, payments, ai, analytics, delivery, rewards, webauthn
from routes.reasoning    import router as reasoning_router
from routes.admin_users  import router as admin_users_router
from routes.notifications import router as notifications_router
from routes.Users         import router as users_router
from routes.appeals       import router as appeals_router              # ← NEW
from routes.billing       import router as billing_router              # ← NEW (ProBite)
# main.py
from routes.social import router as social_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="KotaBites API",
    description="Online Kota Ordering System with Delivery, Wallet, Rewards & Moderation",
    version="2.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,              prefix="/auth",     tags=["Auth"])
app.include_router(menu.router,              prefix="/menu",     tags=["Menu"])
app.include_router(orders.router,            prefix="/orders",   tags=["Orders"])
app.include_router(payments.router,          prefix="/payments", tags=["Payments"])
app.include_router(ai.router,                prefix="/ai",       tags=["AI"])
app.include_router(reasoning_router)
app.include_router(delivery.router,                              tags=["Delivery"])
app.include_router(rewards.router,                               tags=["Rewards"])
app.include_router(webauthn.router,                              tags=["WebAuthn"])
app.include_router(analytics.router,                      tags=["Analytics"])
app.include_router(admin_users_router,                           tags=["Admin — Users"])
app.include_router(notifications_router,                         tags=["Notifications"])
app.include_router(users_router,                                 tags=["Users"])
app.include_router(appeals_router,                               tags=["Appeals"])  # ← NEW
app.include_router(social_router, tags=["Social"])
app.include_router(billing_router, tags=["Billing"])  # ← NEW (ProBite)

@app.get("/")
def home():
    return {
        "message": "KotaBites API v2.3 🔥",
        "features": [
            "User authentication (JWT + Google + GitHub + Spotify)",
            "Menu management", "Order tracking",
            "AI chatbot (OpenRouter)", "AI reasoning (Gemini 2.5 Flash)",
            "Delivery driver system", "Driver wallet management",
            "Customer rewards wallet (KotaPoints)",
            "WebAuthn passkey / fingerprint login",
            "Real-time admin approval",
            "User moderation (suspend · ban · warn · other)",
            "Admin push notifications",
            "Account status & feature gating",
            "User appeal system",                                  # ← NEW
            "ProBite subscriptions (Paystack) + KotaBot credit metering",  # ← NEW
        ]
    }
