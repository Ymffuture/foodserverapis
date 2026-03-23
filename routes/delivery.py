# routes/delivery.py
import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status

from dependencies import get_current_user, get_current_admin_user
from models.user import User
from models.delivery_driver import DeliveryDriver, DriverStatus, VehicleType
from models.wallet_transaction import WalletTransaction, TransactionType, TransactionStatus
from models.delivery_assignment import DeliveryAssignment, AssignmentStatus
from models.order import Order
from utils.enums import OrderStatus
from schemas.delivery_schema import (
    DriverSignupRequest,
    DriverSignupResponse,
    DriverProfileResponse,
    UpdateDriverProfile,
    ToggleAvailability,
    AdminApprovalRequest,
    PendingDriverResponse,
    WalletBalance,
    WithdrawalRequest,
    TransactionResponse,
    AdminAdjustment,
    AvailableOrderResponse,
    AcceptOrderRequest,
    UpdateDeliveryStatus,
    RateDriver,
)
from services.cloudinary_service import upload_image

router = APIRouter(prefix="/delivery", tags=["Delivery"])
logger = logging.getLogger(__name__)

# Statuses that are permitted to toggle availability and request withdrawals.
ONLINE_ELIGIBLE_STATUSES = {
    DriverStatus.APPROVED,
    DriverStatus.ACTIVE,
    DriverStatus.OFFLINE,
}


# ── Helper Functions ───────────────────────────────────────────────────────

async def get_driver_by_user(user_id: str) -> Optional[DeliveryDriver]:
    return await DeliveryDriver.find_one(DeliveryDriver.user_id == user_id)


async def create_wallet_transaction(
    driver: DeliveryDriver,
    transaction_type: TransactionType,
    amount: float,
    description: str,
    order_id: Optional[str] = None,
    processed_by: Optional[str] = None,
    notes: Optional[str] = None,
) -> WalletTransaction:
    balance_before = driver.wallet_balance
    balance_after = balance_before + amount

    if balance_after < 0:
        raise HTTPException(status_code=400, detail="Insufficient wallet balance")

    transaction = WalletTransaction(
        driver_id=str(driver.id),
        driver_email=driver.email,
        type=transaction_type,
        amount=amount,
        status=TransactionStatus.COMPLETED,
        balance_before=balance_before,
        balance_after=balance_after,
        order_id=order_id,
        reference=f"TXN-{uuid.uuid4().hex[:12].upper()}",
        description=description,
        notes=notes,
        processed_by=processed_by,
        completed_at=datetime.utcnow(),
    )

    await transaction.insert()

    driver.wallet_balance = balance_after
    if amount > 0:
        driver.total_earned += amount
    else:
        driver.total_withdrawn += abs(amount)

    driver.updated_at = datetime.utcnow()
    await driver.save()

    logger.info(
        f"Wallet tx | Driver: {driver.email} | {transaction_type.value} | "
        f"R{amount:.2f} | Balance: {balance_before:.2f} → {balance_after:.2f}"
    )

    return transaction


# ── Driver Signup & Profile ───────────────────────────────────────────────

@router.post("/signup", response_model=DriverSignupResponse, status_code=201)
async def driver_signup(
    full_name: str = Form(...),
    phone: str = Form(...),
    id_number: str = Form(...),
    vehicle_type: str = Form(...),
    vehicle_registration: Optional[str] = Form(None),
    drivers_license: Optional[str] = Form(None),
    street_address: str = Form(...),
    suburb: str = Form(...),
    postal_code: str = Form(...),
    bank_name: Optional[str] = Form(None),
    account_number: Optional[str] = Form(None),
    account_holder: Optional[str] = Form(None),
    id_document: Optional[UploadFile] = File(None),
    license_document: Optional[UploadFile] = File(None),
    vehicle_document: Optional[UploadFile] = File(None),
    profile_photo: Optional[UploadFile] = File(None),
    current_user: User = Depends(get_current_user),
):
    existing = await get_driver_by_user(str(current_user.id))
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Driver application already exists ({existing.status.value})"
        )

    try:
        vehicle_enum = VehicleType(vehicle_type.lower())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid vehicle type. Allowed: {[v.value for v in VehicleType]}"
        )

    id_url = await upload_image(id_document) if id_document else None
    license_url = await upload_image(license_document) if license_document else None
    vehicle_url = await upload_image(vehicle_document) if vehicle_document else None
    photo_url = await upload_image(profile_photo) if profile_photo else None

    driver = DeliveryDriver(
        user_id=str(current_user.id),
        email=current_user.email,
        full_name=full_name,
        phone=phone,
        id_number=id_number,
        vehicle_type=vehicle_enum,
        vehicle_registration=vehicle_registration,
        drivers_license=drivers_license,
        id_document_url=id_url,
        license_document_url=license_url,
        vehicle_document_url=vehicle_url,
        profile_photo_url=photo_url,
        street_address=street_address,
        suburb=suburb,
        postal_code=postal_code,
        bank_name=bank_name,
        account_number=account_number,
        account_holder=account_holder,
        status=DriverStatus.PENDING,
    )

    await driver.insert()
    logger.info(f"New driver application: {driver.email} (ID: {driver.id})")

    return DriverSignupResponse(
        id=str(driver.id),
        email=driver.email,
        full_name=driver.full_name,
        status=driver.status.value,
        message="Application submitted. Admin review expected within 24-48 hours.",
        created_at=driver.created_at,
    )


@router.get("/profile", response_model=DriverProfileResponse)
async def get_driver_profile(current_user: User = Depends(get_current_user)):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "No driver profile found. Please sign up first.")

    return DriverProfileResponse(
        id=str(driver.id),
        email=driver.email,
        full_name=driver.full_name,
        phone=driver.phone,
        vehicle_type=driver.vehicle_type.value,
        status=driver.status.value,
        wallet_balance=driver.wallet_balance,
        total_earned=driver.total_earned,
        total_deliveries=driver.total_deliveries,
        rating=driver.rating,
        is_available=driver.is_available,
        created_at=driver.created_at,
        approval_date=driver.approval_date,
        profile_photo_url=driver.profile_photo_url,
    )


@router.patch("/profile", response_model=DriverProfileResponse)
async def update_driver_profile(
    updates: UpdateDriverProfile,
    current_user: User = Depends(get_current_user),
):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    update_data = updates.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(driver, field, value)

    driver.updated_at = datetime.utcnow()
    await driver.save()

    # FIX Bug 2: from_orm() was removed in Pydantic v2 — use model_validate() instead.
    # Also requires model_config = {"from_attributes": True} on DriverProfileResponse (fixed in schema).
    return DriverProfileResponse.model_validate(driver)


@router.post("/toggle-availability")
async def toggle_availability(
    body: ToggleAvailability,
    current_user: User = Depends(get_current_user),
):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    if driver.status not in ONLINE_ELIGIBLE_STATUSES:
        raise HTTPException(403, f"Cannot change availability. Status: {driver.status.value}")

    driver.is_available = body.is_available
    if body.is_available:
        driver.last_online = datetime.utcnow()

    driver.updated_at = datetime.utcnow()
    await driver.save()

    return {
        "is_available": driver.is_available,
        "message": "You are now online" if driver.is_available else "You are now offline"
    }


# ── Admin Endpoints ────────────────────────────────────────────────────────

@router.get("/admin/pending", response_model=List[PendingDriverResponse])
async def get_pending_drivers(admin_user: User = Depends(get_current_admin_user)):
    drivers = await DeliveryDriver.find(
        DeliveryDriver.status == DriverStatus.PENDING
    ).sort("-created_at").to_list()

    return [
        PendingDriverResponse(
            id=str(d.id),
            full_name=d.full_name,
            email=d.email,
            phone=d.phone,
            id_number=d.id_number,
            vehicle_type=d.vehicle_type.value,
            street_address=d.street_address,
            suburb=d.suburb,
            created_at=d.created_at,
            id_document_url=d.id_document_url,
            license_document_url=d.license_document_url,
            vehicle_document_url=d.vehicle_document_url,
            profile_photo_url=d.profile_photo_url,
        )
        for d in drivers
    ]


@router.post("/admin/approve")
async def approve_driver(
    body: AdminApprovalRequest,
    admin_user: User = Depends(get_current_admin_user),
):
    driver = await DeliveryDriver.get(body.driver_id)
    if not driver:
        raise HTTPException(404, "Driver not found")

    if driver.status != DriverStatus.PENDING:
        raise HTTPException(400, f"Driver already {driver.status.value}")

    if body.approved:
        driver.status = DriverStatus.APPROVED
        driver.approval_date = datetime.utcnow()
        driver.approved_by = str(admin_user.id)
        message = f"Driver {driver.full_name} approved"
    else:
        if not body.reason:
            raise HTTPException(422, "Rejection reason required")
        driver.status = DriverStatus.REJECTED
        driver.rejected_reason = body.reason
        message = f"Driver {driver.full_name} rejected: {body.reason}"

    driver.updated_at = datetime.utcnow()
    await driver.save()

    logger.info(f"Admin {admin_user.email} → Driver {body.driver_id} {'approved' if body.approved else 'rejected'}")

    return {"success": True, "message": message, "status": driver.status.value}


@router.get("/admin/all-drivers")
async def get_all_drivers(
    status: Optional[str] = None,
    admin_user: User = Depends(get_current_admin_user),
):
    query = {}
    if status:
        try:
            query["status"] = DriverStatus(status.lower())
        except ValueError:
            pass

    drivers = await DeliveryDriver.find(query).sort("-created_at").to_list()

    return [
        {
            "id": str(d.id),
            "full_name": d.full_name,
            "email": d.email,
            "phone": d.phone,
            "vehicle_type": d.vehicle_type.value,
            "status": d.status.value,
            "wallet_balance": d.wallet_balance,
            "total_earned": d.total_earned,
            "total_deliveries": d.total_deliveries,
            "rating": d.rating,
            "is_available": d.is_available,
            "created_at": d.created_at,
            "approval_date": d.approval_date,
        }
        for d in drivers
    ]


# ── Wallet Operations ──────────────────────────────────────────────────────

@router.get("/wallet/balance", response_model=WalletBalance)
async def get_wallet_balance(current_user: User = Depends(get_current_user)):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    pending_assignments = await DeliveryAssignment.find(
        DeliveryAssignment.driver_id == str(driver.id),
        DeliveryAssignment.status.in_([
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.PICKED_UP,
            AssignmentStatus.IN_TRANSIT,
        ])
    ).to_list()

    pending_amount = sum(a.delivery_fee for a in pending_assignments)

    return WalletBalance(
        balance=driver.wallet_balance,
        total_earned=driver.total_earned,
        total_withdrawn=driver.total_withdrawn,
        pending_amount=pending_amount,
    )


@router.get("/wallet/transactions", response_model=List[TransactionResponse])
async def get_wallet_transactions(
    limit: int = 50,
    current_user: User = Depends(get_current_user),
):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    transactions = await WalletTransaction.find(
        WalletTransaction.driver_id == str(driver.id)
    ).sort("-created_at").limit(limit).to_list()

    return [
        TransactionResponse(
            id=str(t.id),
            type=t.type.value,
            amount=t.amount,
            status=t.status.value,
            description=t.description,
            balance_after=t.balance_after,
            created_at=t.created_at,
            order_id=t.order_id,
        )
        for t in transactions
    ]


@router.post("/wallet/withdraw")
async def request_withdrawal(
    body: WithdrawalRequest,
    current_user: User = Depends(get_current_user),
):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    # FIX Bug 5 (redundant guard): ONLINE_ELIGIBLE_STATUSES already contains APPROVED,
    # so the old `and driver.status != DriverStatus.APPROVED` was unreachable dead code.
    if driver.status not in ONLINE_ELIGIBLE_STATUSES:
        raise HTTPException(403, "Only approved drivers can withdraw")

    if body.amount > driver.wallet_balance:
        raise HTTPException(400, f"Insufficient balance (available: R{driver.wallet_balance:.2f})")

    if body.amount < 50.0:
        raise HTTPException(400, "Minimum withdrawal is R50.00")

    transaction = await create_wallet_transaction(
        driver=driver,
        transaction_type=TransactionType.WITHDRAWAL,
        amount=-body.amount,
        description=f"Withdrawal to {body.bank_name} ••••{body.account_number[-4:]}",
        notes=f"Bank: {body.bank_name}, Account: {body.account_number}, Holder: {body.account_holder}",
    )

    transaction.bank_name = body.bank_name
    transaction.account_number = body.account_number
    await transaction.save()

    return {
        "success": True,
        "message": "Withdrawal request submitted. Processing within 24-48 hours.",
        "reference": transaction.reference,
        "amount": body.amount,
        "new_balance": driver.wallet_balance,
    }


@router.post("/admin/wallet/adjust")
async def admin_wallet_adjustment(
    body: AdminAdjustment,
    admin_user: User = Depends(get_current_admin_user),
):
    driver = await DeliveryDriver.get(body.driver_id)
    if not driver:
        raise HTTPException(404, "Driver not found")

    try:
        trans_type = TransactionType(body.type.lower())
        if trans_type not in [TransactionType.BONUS, TransactionType.PENALTY, TransactionType.ADJUSTMENT]:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "Type must be: bonus, penalty, adjustment")

    transaction = await create_wallet_transaction(
        driver=driver,
        transaction_type=trans_type,
        amount=body.amount,
        description=body.description,
        notes=body.notes,
        processed_by=str(admin_user.id),
    )

    return {
        "success": True,
        "message": f"Wallet adjusted: {body.description}",
        "reference": transaction.reference,
        "new_balance": driver.wallet_balance,
    }


# ── Order Assignment & Delivery ────────────────────────────────────────────

@router.get("/available-orders", response_model=List[AvailableOrderResponse])
async def get_available_orders(current_user: User = Depends(get_current_user)):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    if not driver.is_available:
        raise HTTPException(403, "Go online to see available orders")

    orders = await Order.find(
        Order.status == OrderStatus.READY
    ).sort("-created_at").limit(20).to_list()

    assigned_ids = {
        a.order_id async for a in DeliveryAssignment.find(
            DeliveryAssignment.status.in_([
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.PICKED_UP,
                AssignmentStatus.IN_TRANSIT,
            ])
        )
    }

    available = [o for o in orders if str(o.id) not in assigned_ids]

    result = []
    for order in available:
        customer = await User.get(order.user_id)
        result.append(
            AvailableOrderResponse(
                order_id=str(order.id),
                short_id=str(order.id)[-8:].upper(),
                customer_name=customer.full_name if customer else "Customer",
                delivery_address=order.delivery_address,
                total_amount=order.total_amount,
                delivery_fee=order.delivery_fee or 15.0,
                distance_km=None,
                created_at=order.created_at,
            )
        )

    return result


@router.post("/accept-order")
async def accept_order(
    body: AcceptOrderRequest,
    current_user: User = Depends(get_current_user),
):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    if not driver.is_available:
        raise HTTPException(403, "Must be online to accept orders")

    if driver.current_order_id:
        raise HTTPException(400, "Complete current delivery first")

    order = await Order.get(body.order_id)
    if not order:
        raise HTTPException(404, "Order not found")

    if order.status != OrderStatus.READY:
        raise HTTPException(400, f"Order not ready ({order.status.value})")

    existing = await DeliveryAssignment.find_one(
        DeliveryAssignment.order_id == str(order.id),
        DeliveryAssignment.status.in_([
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.PICKED_UP,
            AssignmentStatus.IN_TRANSIT,
        ])
    )
    if existing:
        raise HTTPException(409, "Order already accepted by another driver")

    customer = await User.get(order.user_id)

    assignment = DeliveryAssignment(
        order_id=str(order.id),
        driver_id=str(driver.id),
        driver_name=driver.full_name,
        driver_phone=driver.phone,
        customer_name=customer.full_name if customer else "Customer",
        customer_phone=order.phone or (customer.phone if customer else ""),
        delivery_address=order.delivery_address,
        delivery_fee=order.delivery_fee or 15.0,
        status=AssignmentStatus.ACCEPTED,
        accepted_at=datetime.utcnow(),
    )
    await assignment.insert()

    driver.current_order_id = str(order.id)
    driver.updated_at = datetime.utcnow()
    await driver.save()

    logger.info(f"Order {order.id} accepted by {driver.email}")

    return {
        "success": True,
        "message": "Order accepted. Proceed to pickup.",
        "assignment_id": str(assignment.id),
        "short_id": str(order.id)[-8:].upper(),
        "delivery_address": order.delivery_address,
        "customer_phone": assignment.customer_phone,
        "delivery_fee": assignment.delivery_fee,
    }


@router.patch("/update-delivery-status")
async def update_delivery_status(
    body: UpdateDeliveryStatus,
    current_user: User = Depends(get_current_user),
):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    assignment = await DeliveryAssignment.get(body.assignment_id)
    if not assignment or assignment.driver_id != str(driver.id):
        raise HTTPException(403, "Not your delivery or not found")

    try:
        new_status = AssignmentStatus(body.status)
    except ValueError:
        raise HTTPException(422, f"Invalid status: {body.status}")

    assignment.status = new_status
    assignment.notes = body.notes
    now = datetime.utcnow()

    if new_status == AssignmentStatus.PICKED_UP:
        assignment.picked_up_at = now
    elif new_status == AssignmentStatus.DELIVERED:
        assignment.delivered_at = now
        if assignment.accepted_at:
            assignment.actual_time = int((now - assignment.accepted_at).total_seconds() / 60)

        order = await Order.get(assignment.order_id)
        if order:
            order.status = OrderStatus.DELIVERED
            await order.save()

        await create_wallet_transaction(
            driver=driver,
            transaction_type=TransactionType.DELIVERY_PAYMENT,
            amount=assignment.delivery_fee,
            description=f"Delivery fee - Order #{str(assignment.order_id)[-8:].upper()}",
            order_id=assignment.order_id,
        )

        driver.total_deliveries += 1
        driver.current_order_id = None
        await driver.save()

        logger.info(
            f"Delivery completed | Order: {assignment.order_id} | "
            f"Driver: {driver.email} | Fee: R{assignment.delivery_fee:.2f}"
        )

    await assignment.save()

    return {
        "success": True,
        "message": f"Status updated to {new_status.value}",
        "status": new_status.value,
    }


@router.get("/active-delivery")
async def get_active_delivery(current_user: User = Depends(get_current_user)):
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(404, "Driver profile not found")

    if not driver.current_order_id:
        return {"active": False, "message": "No active delivery"}

    assignment = await DeliveryAssignment.find_one(
        DeliveryAssignment.order_id == driver.current_order_id,
        DeliveryAssignment.driver_id == str(driver.id),
    )

    if not assignment:
        driver.current_order_id = None
        await driver.save()
        return {"active": False, "message": "No active delivery (stale record cleared)"}

    order = await Order.get(assignment.order_id)

    return {
        "active": True,
        "assignment_id": str(assignment.id),
        "order_id": assignment.order_id,
        "short_id": str(assignment.order_id)[-8:].upper(),
        "status": assignment.status.value,
        "customer_name": assignment.customer_name,
        "customer_phone": assignment.customer_phone,
        "delivery_address": assignment.delivery_address,
        "delivery_fee": assignment.delivery_fee,
        "total_amount": order.total_amount if order else 0,
        "accepted_at": assignment.accepted_at,
        "picked_up_at": assignment.picked_up_at,
    }


@router.post("/rate-driver")
async def rate_driver(
    body: RateDriver,
    current_user: User = Depends(get_current_user),
):
    assignment = await DeliveryAssignment.get(body.assignment_id)
    if not assignment:
        raise HTTPException(404, "Delivery not found")

    order = await Order.get(assignment.order_id)
    if not order or order.user_id != str(current_user.id):
        raise HTTPException(403, "Not your order")

    if assignment.status != AssignmentStatus.DELIVERED:
        raise HTTPException(400, "Can only rate completed deliveries")

    if assignment.rating is not None:
        raise HTTPException(400, "Already rated this delivery")

    assignment.rating = body.rating
    assignment.rating_comment = body.comment
    await assignment.save()

    driver = await DeliveryDriver.get(assignment.driver_id)
    if driver:
        total_points = driver.rating * driver.total_ratings
        new_count = driver.total_ratings + 1
        new_avg = (total_points + body.rating) / new_count
        driver.rating = round(new_avg, 2)
        driver.total_ratings = new_count
        await driver.save()

        logger.info(
            f"Driver {driver.email} rated {body.rating}/5 → New avg: {driver.rating} "
            f"({driver.total_ratings} ratings)"
        )

    return {
        "success": True,
        "message": "Thank you for your rating!",
        "rating": body.rating,
    }


# ── Customer: Delivery Info for Order ──────────────────────────────────────

@router.get("/assignment/order/{order_id}")
async def get_order_delivery_info(
    order_id: str,
    current_user: User = Depends(get_current_user),
):
    order = await Order.get(order_id)
    if not order or order.user_id != str(current_user.id):
        raise HTTPException(404, "Order not found or not yours")

    assignment = await DeliveryAssignment.find_one(
        DeliveryAssignment.order_id == order_id,
        DeliveryAssignment.status.in_([
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.PICKED_UP,
            AssignmentStatus.IN_TRANSIT,
            AssignmentStatus.DELIVERED,
        ])
    )

    if not assignment:
        return {"has_driver": False, "message": "No driver assigned yet"}

    driver = await DeliveryDriver.get(assignment.driver_id)

    return {
        "has_driver": True,
        "driver_name": assignment.driver_name,
        "driver_phone": assignment.driver_phone,
        "driver_vehicle": driver.vehicle_type.value if driver else None,
        "status": assignment.status.value,
        "delivery_fee": assignment.delivery_fee,
        "accepted_at": assignment.accepted_at,
        "picked_up_at": assignment.picked_up_at,
        "delivered_at": assignment.delivered_at,
        "actual_time_minutes": assignment.actual_time,
    }
