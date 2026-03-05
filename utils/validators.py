import re

def validate_phone(phone: str):
    pattern = r"^[0-9]{10,13}$"
    return re.match(pattern, phone)
