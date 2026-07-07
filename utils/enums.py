from enum import Enum

class OrderStatus(str, Enum):
    SCHEDULED = "scheduled"   # placed ahead of time — flips to PENDING when scheduled_for arrives
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
