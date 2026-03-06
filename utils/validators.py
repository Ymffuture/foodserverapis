from fastapi import HTTPException

def validate_phone(phone: str):
    if not phone.startswith("0") or len(phone) != 10:
        raise HTTPException(status_code=400, detail="Phone number must be 10 digits starting with 0")
    return phone
