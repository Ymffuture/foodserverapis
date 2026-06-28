from enum import Enum

class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    PREPARING = "preparing"
    READY = "ready"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class SubscriptionPlan(str, Enum):
    FREE    = "free"
    PROBITE = "probite"


class BillingCycle(str, Enum):
    MONTHLY = "monthly"
    YEARLY  = "yearly"


class SubscriptionStatus(str, Enum):
    ACTIVE    = "active"     # paid + within current period
    CANCELLED = "cancelled"  # user cancelled — stays PROBITE until expires_at, then auto-downgrades
    EXPIRED   = "expired"    # past expires_at, downgraded to FREE
    NONE      = "none"       # never subscribed (FREE plan, default)
