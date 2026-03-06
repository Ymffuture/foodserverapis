
import requests
from config import PAYSTACK_SECRET_KEY

HEADERS = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}

def initialize_payment(email: str, amount: int, reference: str):
    url = "https://api.paystack.co/transaction/initialize"
    data = {
        "email": email,
        "amount": amount * 100,  # Paystack uses kobo
        "reference": reference,
        "currency": "ZAR"
    }
    response = requests.post(url, json=data, headers=HEADERS)
    return response.json()

def verify_payment(reference: str):
    url = f"https://api.paystack.co/transaction/verify/{reference}"
    response = requests.get(url, headers=HEADERS)
    return response.json()
