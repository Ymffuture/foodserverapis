# main.py
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine
from .routes import auth, menu, orders, payments

# Create all tables (only recommended during development or first deploy)
# In production → use Alembic migrations instead
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="KotaBites API",
    description="API for KotaBites food ordering platform",
    version="1.0.0",
    docs_url="/docs",           # keep Swagger UI
    redoc_url="/redoc",         # optional: keep ReDoc too
    openapi_url="/openapi.json" # keep for clients
)

# CORS – very permissive for development
# In production → restrict origins to your actual frontend domain(s)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],                    # ← change this in production!
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include all routers
app.include_router(auth.router, prefix="/auth", tags=["Authentication"])
app.include_router(menu.router, prefix="/menu", tags=["Menu"])
app.include_router(orders.router, prefix="/orders", tags=["Orders"])
app.include_router(payments.router, prefix="/payments", tags=["Payments"])


# Optional: root endpoint for health check / Render
@app.get("/", tags=["Health"])
def root():
    return {"message": "KotaBites API is running 🚀", "status": "healthy"}


# Optional: simple ping endpoint (useful for Render health checks)
@app.get("/health", tags=["Health"])
def health_check():
    return {"status": "ok"}
