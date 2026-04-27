# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from database import init_db
from routes import auth, menu, orders, payments, ai, routes_analytics, delivery, rewards, webauthn
from routers.reasoning import router as reasoning_router

app.include_router(reasoning_router, prefix="/api")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="KotaBites API",
    description="Online Kota Ordering System with Delivery, Wallet & Rewards",
    version="2.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,             prefix="/auth",     tags=["Auth"])
app.include_router(menu.router,             prefix="/menu",     tags=["Menu"])
app.include_router(orders.router,           prefix="/orders",   tags=["Orders"])
app.include_router(payments.router,         prefix="/payments", tags=["Payments"])
app.include_router(ai.router,               prefix="/ai",       tags=["AI"])
app.include_router(delivery.router,                             tags=["Delivery"])
app.include_router(rewards.router,                              tags=["Rewards"])
app.include_router(webauthn.router,                             tags=["WebAuthn"])
app.include_router(routes_analytics.router,                     tags=["Analytics"])


@app.get("/")
def home():
    return {
        "message": "KotaBites API v2.1 🔥",
        "features": [
            "User authentication",
            "Menu management",
            "Order tracking",
            "AI chatbot",
            "Delivery driver system",
            "Driver wallet management",
            "Customer rewards wallet (KotaPoints)",
            "WebAuthn passkey / fingerprint login",
            "Real-time admin approval",
        ]
    }
