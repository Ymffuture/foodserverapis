# models/delivery_driver.py
from beanie import Document
from pydantic import Field, EmailStr
from typing import Optional
from datetime import datetime
from enum import Enum


class DriverStatus(str, Enum):
    PENDING = "pending"           # Waiting for admin approval
    APPROVED = "approved"         # Approved and can take orders
    REJECTED = "rejected"         # Application rejected
    SUSPENDED = "suspended"       # Temporarily suspended
    ACTIVE = "active"             # Currently online and available
    OFFLINE = "offline"           # Approved but not currently working


class VehicleType(str, Enum):
    BICYCLE = "bicycle"
    MOTORCYCLE = "motorcycle"
    CAR = "car"
    SCOOTER = "scooter"


class DeliveryDriver(Document):
    # Personal Information
    user_id: str                              # Link to User model
    email: EmailStr
    full_name: str
    phone: str
    id_number: str                            # South African ID number
    
    # Vehicle Information
    vehicle_type: VehicleType
    vehicle_registration: Optional[str] = None
    drivers_license: Optional[str] = None     # License number
    
    # Documents (Cloudinary URLs)
    id_document_url: Optional[str] = None     # ID photo upload
    license_document_url: Optional[str] = None
    vehicle_document_url: Optional[str] = None
    profile_photo_url: Optional[str] = None
    
    # Address
    street_address: str
    suburb: str
    city: str = "Johannesburg"
    postal_code: str
    
    # Banking Details (for payouts)
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_holder: Optional[str] = None
    
    # Status & Approval
    status: DriverStatus = DriverStatus.PENDING
    approval_date: Optional[datetime] = None
    rejected_reason: Optional[str] = None
    approved_by: Optional[str] = None         # Admin user_id who approved
    
    # Wallet
    wallet_balance: float = Field(default=0.0, ge=0)
    total_earned: float = Field(default=0.0, ge=0)
    total_withdrawn: float = Field(default=0.0, ge=0)
    
    # Performance Metrics
    total_deliveries: int = Field(default=0, ge=0)
    rating: float = Field(default=5.0, ge=0, le=5)
    total_ratings: int = Field(default=0, ge=0)
    
    # Availability
    is_available: bool = False                # Currently accepting orders
    current_order_id: Optional[str] = None    # Active delivery
    
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    last_online: Optional[datetime] = None

    class Settings:
        name = "delivery_drivers"
        indexes = [
            "user_id",
            "email",
            "phone",
            "status",
            "is_available",
            [("status", 1), ("is_available", 1)],  # For finding available drivers
        ]
