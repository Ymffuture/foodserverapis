# models/delivery_assignment.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from enum import Enum
from typing import Optional


class AssignmentStatus(str, Enum):
    PENDING = "pending"           # Order ready, waiting for driver
    ACCEPTED = "accepted"         # Driver accepted
    PICKED_UP = "picked_up"       # Driver picked up from restaurant
    IN_TRANSIT = "in_transit"     # On the way to customer
    DELIVERED = "delivered"       # Successfully delivered
    FAILED = "failed"             # Delivery failed
    CANCELLED = "cancelled"       # Assignment cancelled


class DeliveryAssignment(Document):
    order_id: str
    driver_id: str
    driver_name: str
    driver_phone: str
    
    # Customer Info (cached for quick access)
    customer_name: str
    customer_phone: str
    delivery_address: str
    
    # Assignment Details
    status: AssignmentStatus = AssignmentStatus.PENDING
    delivery_fee: float = Field(default=15.0, ge=0)  # Driver's earning
    distance_km: Optional[float] = None
    
    # Timestamps
    assigned_at: datetime = Field(default_factory=datetime.utcnow)
    accepted_at: Optional[datetime] = None
    picked_up_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    
    # Performance
    estimated_time: Optional[int] = None      # Minutes
    actual_time: Optional[int] = None         # Minutes
    
    # Rating (customer rates driver)
    rating: Optional[float] = Field(default=None, ge=0, le=5)
    rating_comment: Optional[str] = None
    
    # Issues
    failure_reason: Optional[str] = None
    notes: Optional[str] = None

    class Settings:
        name = "delivery_assignments"
        indexes = [
            "order_id",
            "driver_id",
            "status",
            "assigned_at",
        ]
