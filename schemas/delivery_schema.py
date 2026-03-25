# schemas/delivery_schema.py
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class DriverSignupRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    phone: str = Field(..., pattern=r"^0\d{9}$")
    id_number: str = Field(..., min_length=13, max_length=13)
    vehicle_type: str
    vehicle_registration: Optional[str] = None
    drivers_license: Optional[str] = None
    street_address: str
    suburb: str
    postal_code: str
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_holder: Optional[str] = None


class DriverSignupResponse(BaseModel):
    id: str
    email: str
    full_name: str
    status: str
    message: str
    created_at: datetime


class DriverProfileResponse(BaseModel):
    id: str
    email: str
    full_name: str
    phone: str
    vehicle_type: str
    status: str
    wallet_balance: float
    total_earned: float
    total_deliveries: int
    rating: float
    is_available: bool
    created_at: datetime
    approval_date: Optional[datetime] = None
    profile_photo_url: Optional[str] = None
    # ✅ FIX: Banking fields were missing — Pydantic stripped them from every
    # /delivery/profile response, so Wallet.jsx always saw undefined for
    # bank_name/account_number/account_holder → canWithdraw was always false.
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_holder: Optional[str] = None

    model_config = {"from_attributes": True}


class UpdateDriverProfile(BaseModel):
    phone: Optional[str] = None
    street_address: Optional[str] = None
    suburb: Optional[str] = None
    postal_code: Optional[str] = None
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    account_holder: Optional[str] = None


class ToggleAvailability(BaseModel):
    is_available: bool


class AdminApprovalRequest(BaseModel):
    driver_id: str
    approved: bool
    reason: Optional[str] = None


class PendingDriverResponse(BaseModel):
    id: str
    full_name: str
    email: str
    phone: str
    id_number: str
    vehicle_type: str
    street_address: str
    suburb: str
    created_at: datetime
    id_document_url: Optional[str] = None
    license_document_url: Optional[str] = None
    vehicle_document_url: Optional[str] = None
    profile_photo_url: Optional[str] = None


class WalletBalance(BaseModel):
    balance: float
    total_earned: float
    total_withdrawn: float
    pending_amount: float


class WithdrawalRequest(BaseModel):
    amount: float = Field(..., gt=0)
    bank_name: str
    account_number: str
    account_holder: str


class TransactionResponse(BaseModel):
    id: str
    type: str
    amount: float
    status: str
    description: str
    balance_after: float
    created_at: datetime
    order_id: Optional[str] = None


class AdminAdjustment(BaseModel):
    driver_id: str
    amount: float
    type: str
    description: str
    notes: Optional[str] = None


# ✅ FIX: Added phone, items, and payment_method.
# The old schema only had order_id/address/total/fee/distance/created_at.
# Pydantic silently strips any field not declared here, so even though
# get_available_orders() was building phone/items/payment_method, they
# were dropped before the response left the server.
class AvailableOrderResponse(BaseModel):
    order_id: str
    short_id: str
    customer_name: str
    phone: Optional[str] = None           # Customer contact for driver
    delivery_address: str
    total_amount: float
    delivery_fee: float
    payment_method: Optional[str] = None  # "cash" | "paystack" — driver needs to know
    items: List[Dict[str, Any]] = []       # [{name, quantity}] — what to collect
    distance_km: Optional[float] = None
    created_at: datetime


class AcceptOrderRequest(BaseModel):
    order_id: str


class UpdateDeliveryStatus(BaseModel):
    assignment_id: str
    status: str
    notes: Optional[str] = None


class RateDriver(BaseModel):
    assignment_id: str
    rating: float = Field(..., ge=1, le=5)
    comment: Optional[str] = None
