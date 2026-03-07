from beanie import Document
from pydantic import EmailStr

class User(Document):
    email: EmailStr
    hashed_password: str
    full_name: str
    phone: str

    class Settings:
        name = "users"
