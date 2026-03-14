# main.py
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
    description="Online Kota Ordering System",
    version="1.0.0",
    lifespan=lifespan,
)

# ✅ FIX: Auth is done via Authorization: Bearer token in headers, NOT cookies.
# Therefore allow_credentials MUST be False and allow_origins can be ["*"].
#
# Old config had allow_credentials=True with specific origins. This is only needed
# when using cookie-based auth. With Bearer tokens we don't need credentials mode,
# so wildcard origins work fine and won't cause CORS preflight failures.
#
# This also fixes local development (localhost:5173 was being blocked).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routes import auth, menu, orders, payments

app.include_router(auth.router,     prefix="/auth",     tags=["Auth"])
app.include_router(menu.router,     prefix="/menu",     tags=["Menu"])
app.include_router(orders.router,   prefix="/orders",   tags=["Orders"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])


@app.get("/")
def home():
    return {"message": "KotaBites API is live 🔥"}
