# backend/main.py
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
import time
import logging

from database import init_db, close_db

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
from routes.users import router as users_router
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
    await close_db()


app = FastAPI(
    title="KotaBites API",
    description="Food delivery + social + wallet system",
    version="2.3.0",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────
# CORS (LOCK THIS IN PRODUCTION)
# ─────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "https://foodsorder.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────
# REQUEST LOGGING
# ─────────────────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start

    logging.info(
        f"{request.method} {request.url.path} - {response.status_code} ({duration:.2f}s)"
    )

    return response


# ─────────────────────────────────────────────
# GLOBAL ERROR HANDLER
# ─────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    logging.exception("Unhandled error")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "Internal server error"
        },
    )


# ─────────────────────────────────────────────
# ROUTERS (VERSIONED API)
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
# HEALTH CHECK
# ─────────────────────────────────────────────

@app.get("/")
def root():
    return {
        "message": "KotaBites API is running 🚀",
        "version": "2.3.0",
        "status": "healthy",
        "docs": "/docs",
    }
