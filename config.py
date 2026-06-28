# config.py
from dotenv import load_dotenv
import os

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
SECRET_KEY = os.getenv("JWT_SECRET", "changeme")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60))

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY")

# ── ProBite pricing ──────────────────────────────────────────────────────────
# PLACEHOLDER prices — swap these once pricing is finalised, nothing else
# needs to change. Whole Rand amounts (Paystack wants an int, converted to
# cents/kobo in paystack_service.py).
PROBITE_PRICE_MONTHLY_ZAR = int(os.getenv("PROBITE_PRICE_MONTHLY_ZAR", 49))
PROBITE_PRICE_YEARLY_ZAR  = int(os.getenv("PROBITE_PRICE_YEARLY_ZAR", 499))  # ~15% off vs 12×monthly

# Plan codes from Paystack dashboard (Settings → Plans) or services.paystack_service.create_plan().
# Subscriptions can't go live until these are set.
PAYSTACK_PLAN_CODE_MONTHLY = os.getenv("PAYSTACK_PLAN_CODE_MONTHLY")
PAYSTACK_PLAN_CODE_YEARLY  = os.getenv("PAYSTACK_PLAN_CODE_YEARLY")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

# GitHub OAuth
GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")

# Spotify OAuth
SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
