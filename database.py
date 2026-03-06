# database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# Import DATABASE_URL from config (should be something like postgresql://... or sqlite:///...)
from config import DATABASE_URL

# ────────────────────────────────────────────────
# Engine configuration
# ────────────────────────────────────────────────
engine = create_engine(
    DATABASE_URL,
    # Only needed for SQLite when using sync SQLAlchemy in multi-threaded environments
    # Safe to keep — has no effect on PostgreSQL/MySQL
    connect_args={"check_same_thread": False},
    
    # Recommended production additions:
    # pool_size=20,           # max connections in pool
    # max_overflow=10,        # extra connections allowed
    # pool_timeout=30,        # seconds to wait for connection
    # pool_pre_ping=True,     # test connections before use (helps with stale connections)
)

# ────────────────────────────────────────────────
# Session factory
# ────────────────────────────────────────────────
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    # Optional but useful: expire_on_commit=False → objects remain usable after commit
    # expire_on_commit=False,
)

# ────────────────────────────────────────────────
# Base class for all models
# ────────────────────────────────────────────────
Base = declarative_base()


# ────────────────────────────────────────────────
# Dependency for FastAPI (recommended pattern)
# ────────────────────────────────────────────────
def get_db():
    """
    FastAPI dependency to get a database session.
    Usage in routes:
    
    @app.get("/items/")
    def read_items(db: Session = Depends(get_db)):
        ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
