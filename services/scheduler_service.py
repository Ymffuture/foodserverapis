# services/scheduler_service.py
"""
Runs a lightweight background job that promotes SCHEDULED orders ("order for
6pm") to PENDING the moment their requested time arrives, so the kitchen
picks them up through the normal flow without any special-casing elsewhere.

Uses APScheduler's AsyncIOScheduler (runs inside the same event loop FastAPI
already uses — no separate process/worker needed).
"""
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from models.order import Order
from utils.enums import OrderStatus
from datetime import datetime

logger = logging.getLogger("scheduler")
scheduler = AsyncIOScheduler()


async def activate_due_scheduled_orders():
    """Find every SCHEDULED order whose time has arrived and flip it to PENDING."""
    try:
        due_orders = await Order.find({
            "status": OrderStatus.SCHEDULED.value,
            "scheduled_for": {"$lte": datetime.utcnow()},
        }).to_list()

        for order in due_orders:
            order.status = OrderStatus.PENDING
            await order.save()
            logger.info(f"Activated scheduled order {order.id} (was due {order.scheduled_for})")

        if due_orders:
            logger.info(f"Activated {len(due_orders)} scheduled order(s)")
    except Exception as e:
        # Never let a bad tick crash the scheduler — log and try again next interval
        logger.error(f"activate_due_scheduled_orders failed: {e}")


def start_scheduler():
    scheduler.add_job(
        activate_due_scheduled_orders,
        trigger=IntervalTrigger(minutes=1),
        id="activate_scheduled_orders",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — checking for due scheduled orders every minute")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
