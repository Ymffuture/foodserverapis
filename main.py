# main.py  (updated — add AI router + register Suggestion in Beanie)
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routes import auth, menu, orders, payments, ai, routes_analytics # ← add ai


app.include_router(auth.router,     prefix="/auth",     tags=["Auth"])
app.include_router(menu.router,     prefix="/menu",     tags=["Menu"])
app.include_router(orders.router,   prefix="/orders",   tags=["Orders"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])
app.include_router(ai.router,       prefix="/ai",       tags=["AI"])   # ← new
app.include_router(routes_analytics.router, tags=["Analytics"])

@app.get("/")
def home():
    return {"message": "KotaBites API is live 🔥"}
