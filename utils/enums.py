from enum import Enum

class OrderStatus(str, Enum):
    submitted = "submitted"
    pending = "pending"
    approved = "approved"
    declined = "declined"
    preparing = "preparing"
    on_delivery = "on_delivery"
    delivered = "delivered"
    closed = "closed"
