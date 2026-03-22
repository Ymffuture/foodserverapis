# main.py  (updated — add delivery router)
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="KotaBites API",
    description="Online Kota Ordering System with Delivery & Wallet",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routes import auth, menu, orders, payments, ai, routes_analytics, delivery  # ← add delivery


app.include_router(auth.router,              prefix="/auth",     tags=["Auth"])
app.include_router(menu.router,              prefix="/menu",     tags=["Menu"])
app.include_router(orders.router,            prefix="/orders",   tags=["Orders"])
app.include_router(payments.router,          prefix="/payments", tags=["Payments"])
app.include_router(ai.router,                prefix="/ai",       tags=["AI"])
app.include_router(delivery.router,           tags=["Delivery"])  # ← FIXED: added prefix
app.include_router(routes_analytics.router,  tags=["Analytics"])

@app.get("/")
def home():
    return {
        "message": "KotaBites API v2.0 🔥",
        "features": [
            "User authentication",
            "Menu management",
            "Order tracking",
            "AI chatbot",
            "Delivery driver system",  # ← new
            "Wallet management",        # ← new
            "Real-time admin approval"  # ← new
        ]
    }
