
# ── Customer: Get delivery info for a specific order ────────────────────────
# Add this route at the END of routes/delivery.py

@router.get("/assignment/order/{order_id}")
async def get_order_delivery_info(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    """
    Customer endpoint: returns driver info + delivery status for their order.
    Used by OrderStatus page to show who is delivering.
    """
    # Verify order belongs to this user
    order = await Order.get(order_id)
    if not order or order.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Order not found")

    assignment = await DeliveryAssignment.find_one(
        DeliveryAssignment.order_id == str(order.id),
        DeliveryAssignment.status.in_([
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.PICKED_UP,
            AssignmentStatus.IN_TRANSIT,
            AssignmentStatus.DELIVERED,
        ])
    )

    if not assignment:
        return {"has_driver": False}

    return {
        "has_driver":   True,
        "driver_name":  assignment.driver_name,
        "driver_phone": assignment.driver_phone,
        "status":       assignment.status.value,
        "delivery_fee": assignment.delivery_fee,
        "accepted_at":  assignment.accepted_at,
        "picked_up_at": assignment.picked_up_at,
        "delivered_at": assignment.delivered_at,
        "actual_time":  assignment.actual_time,
    }
