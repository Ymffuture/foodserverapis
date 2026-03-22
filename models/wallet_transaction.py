# models/wallet_transaction.py
from beanie import Document
from pydantic import Field
from datetime import datetime
from enum import Enum
from typing import Optional


class TransactionType(str, Enum):
    DELIVERY_PAYMENT = "delivery_payment"     # Earned from delivery
    WITHDRAWAL = "withdrawal"                 # Driver withdrew funds
    BONUS = "bonus"                          # Admin bonus/incentive
    PENALTY = "penalty"                      # Deduction for violation
    REFUND = "refund"                        # Refund to driver
    ADJUSTMENT = "adjustment"                # Manual admin adjustment


class TransactionStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WalletTransaction(Document):
    driver_id: str                           # DeliveryDriver ID
    driver_email: str
    
    # Transaction Details
    type: TransactionType
    amount: float                            # Positive = credit, Negative = debit
    status: TransactionStatus = TransactionStatus.PENDING
    
    # Balance Tracking
    balance_before: float
    balance_after: float
    
    # Reference
    order_id: Optional[str] = None           # If related to an order
    reference: str                           # Unique transaction reference
    
    # Banking (for withdrawals)
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    withdrawal_date: Optional[datetime] = None
    
    # Metadata
    description: str
    notes: Optional[str] = None
    processed_by: Optional[str] = None       # Admin user_id if manual
    
    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    class Settings:
        name = "wallet_transactions"
        indexes = [
            "driver_id",
            "type",
            "status",
            "reference",
            "created_at",
            [("driver_id", 1), ("created_at", -1)],  # For transaction history
        ]
