import requests
from ..config import PAYSTACK_SECRET

BASE = "https://api.paystack.co"

def initialize_payment(email, amount):

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
        "Content-Type": "application/json",
    }

    payload = {
        "email": email,
        "amount": int(amount * 100)
    }

    r = requests.post(
        f"{BASE}/transaction/initialize",
        json=payload,
        headers=headers
    )

    return r.json()


def verify_payment(reference):

    headers = {
        "Authorization": f"Bearer {PAYSTACK_SECRET}",
    }

    r = requests.get(
        f"{BASE}/transaction/verify/{reference}",
        headers=headers
    )

    return r.json()
