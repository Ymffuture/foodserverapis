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

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://foodsorder.vercel.app",
        "https://adminfoods.vercel.app",
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization", "Accept", "Origin",
                   "X-Requested-With", "Access-Control-Request-Method",
                   "Access-Control-Request-Headers"],
)

from routes import auth, menu, orders, payments

app.include_router(auth.router,     prefix="/auth",     tags=["Auth"])
app.include_router(menu.router,     prefix="/menu",     tags=["Menu"])
app.include_router(orders.router,   prefix="/orders",   tags=["Orders"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])


@app.get("/")
def home():
    return {"message": "KotaBites API is live"}
