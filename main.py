from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine
from routes import auth, menu, orders, payments

Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="KotaBites API",
    description="Online Kota Ordering System - South Africa",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Change to your Vercel domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router,     prefix="/auth",     tags=["Auth"])
app.include_router(menu.router,     prefix="/menu",     tags=["Menu"])
app.include_router(orders.router,   prefix="/orders",   tags=["Orders"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])

@app.get("/")
def home():
    return {"message": "KotaBites Backend is Live! 🍔"}
