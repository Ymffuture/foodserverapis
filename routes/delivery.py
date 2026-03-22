# routes/delivery.py

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, status
from pydantic import EmailStr

from dependencies import get_current_user
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


# ── Helper Functions ───────────────────────────────────────────────────────

async def get_driver_by_user(user_id: str) -> Optional[DeliveryDriver]:
    """Get driver profile by user_id"""
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
    """Create a wallet transaction and update driver balance"""
    
    balance_before = driver.wallet_balance
    balance_after = balance_before + amount
    
    if balance_after < 0:
        raise HTTPException(
            status_code=400,
            detail="Insufficient wallet balance"
        )
    
    # Create transaction
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
    
    # Update driver balance
    driver.wallet_balance = balance_after
    
    if amount > 0:
        driver.total_earned += amount
    else:
        driver.total_withdrawn += abs(amount)
    
    driver.updated_at = datetime.utcnow()
    await driver.save()
    
    logger.info(
        f"Wallet transaction: {transaction_type.value} | "
        f"Driver: {driver.email} | Amount: R{amount:.2f} | "
        f"Balance: R{balance_before:.2f} → R{balance_after:.2f}"
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
    """
    Driver signup - creates pending application for admin approval.
    Requires document uploads and banking details.
    """
    
    # Check if user already has a driver account
    existing = await get_driver_by_user(str(current_user.id))
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"You already have a driver application ({existing.status.value})"
        )
    
    # Validate vehicle type
    try:
        vehicle_enum = VehicleType(vehicle_type.lower())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid vehicle type. Must be: {[v.value for v in VehicleType]}"
        )
    
    # Upload documents
    id_url = await upload_image(id_document) if id_document else None
    license_url = await upload_image(license_document) if license_document else None
    vehicle_url = await upload_image(vehicle_document) if vehicle_document else None
    photo_url = await upload_image(profile_photo) if profile_photo else None
    
    # Create driver application
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
    
    logger.info(f"New driver application: {driver.email} ({driver.id})")
    
    return DriverSignupResponse(
        id=str(driver.id),
        email=driver.email,
        full_name=driver.full_name,
        status=driver.status.value,
        message="Application submitted! Admin will review within 24-48 hours.",
        created_at=driver.created_at,
    )


@router.get("/profile", response_model=DriverProfileResponse)
async def get_driver_profile(current_user: User = Depends(get_current_user)):
    """Get current driver's profile"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(
            status_code=404,
            detail="No driver profile found. Please sign up first."
        )
    
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
    """Update driver profile information"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    # Update fields
    update_data = updates.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(driver, field, value)
    
    driver.updated_at = datetime.utcnow()
    await driver.save()
    
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


@router.post("/toggle-availability")
async def toggle_availability(
    body: ToggleAvailability,
    current_user: User = Depends(get_current_user),
):
    """Toggle driver online/offline status"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    if driver.status != DriverStatus.APPROVED:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot go online. Status: {driver.status.value}"
        )
    
    driver.is_available = body.is_available
    driver.last_online = datetime.utcnow() if body.is_available else driver.last_online
    driver.updated_at = datetime.utcnow()
    await driver.save()
    
    return {
        "is_available": driver.is_available,
        "message": "You are now online!" if driver.is_available else "You are now offline"
    }


# ── Admin: Driver Approval ─────────────────────────────────────────────────

@router.get("/admin/pending", response_model=List[PendingDriverResponse])
async def get_pending_drivers(current_user: User = Depends(get_current_user)):
    """Admin: Get all pending driver applications"""
    
    # TODO: Add admin role check
    # if not current_user.is_admin:
    #     raise HTTPException(status_code=403, detail="Admin access required")
    
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
    current_user: User = Depends(get_current_user),
):
    """Admin: Approve or reject driver application"""
    
    # TODO: Add admin role check
    
    driver = await DeliveryDriver.get(body.driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    if driver.status != DriverStatus.PENDING:
        raise HTTPException(
            status_code=400,
            detail=f"Driver is already {driver.status.value}"
        )
    
    if body.approved:
        driver.status = DriverStatus.APPROVED
        driver.approval_date = datetime.utcnow()
        driver.approved_by = str(current_user.id)
        message = f"Driver {driver.full_name} approved!"
        
        # TODO: Send email/notification to driver
        
    else:
        if not body.reason:
            raise HTTPException(
                status_code=422,
                detail="Rejection reason is required"
            )
        
        driver.status = DriverStatus.REJECTED
        driver.rejected_reason = body.reason
        message = f"Driver {driver.full_name} rejected: {body.reason}"
        
        # TODO: Send rejection email with reason
    
    driver.updated_at = datetime.utcnow()
    await driver.save()
    
    logger.info(
        f"Driver application {body.driver_id} "
        f"{'approved' if body.approved else 'rejected'} by {current_user.email}"
    )
    
    return {"success": True, "message": message, "status": driver.status.value}


@router.get("/admin/all-drivers")
async def get_all_drivers(
    status: Optional[str] = None,
    current_user: User = Depends(get_current_user),
):
    """Admin: Get all drivers with optional status filter"""
    
    # TODO: Add admin check
    
    query = {}
    if status:
        try:
            query["status"] = DriverStatus(status)
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
    """Get driver's wallet balance"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    # Calculate pending amount (from accepted but not yet paid deliveries)
    pending = await DeliveryAssignment.find(
        DeliveryAssignment.driver_id == str(driver.id),
        DeliveryAssignment.status.in_([
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.PICKED_UP,
            AssignmentStatus.IN_TRANSIT,
        ])
    ).to_list()
    
    pending_amount = sum(d.delivery_fee for d in pending)
    
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
    """Get driver's transaction history"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
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
    """Request withdrawal from wallet"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    if driver.status != DriverStatus.APPROVED:
        raise HTTPException(
            status_code=403,
            detail="Only approved drivers can withdraw funds"
        )
    
    if body.amount > driver.wallet_balance:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient balance. Available: R{driver.wallet_balance:.2f}"
        )
    
    min_withdrawal = 50.0
    if body.amount < min_withdrawal:
        raise HTTPException(
            status_code=400,
            detail=f"Minimum withdrawal amount is R{min_withdrawal:.2f}"
        )
    
    # Create withdrawal transaction
    transaction = await create_wallet_transaction(
        driver=driver,
        transaction_type=TransactionType.WITHDRAWAL,
        amount=-body.amount,  # Negative for debit
        description=f"Withdrawal to {body.bank_name} - {body.account_number[-4:]}",
        notes=f"Bank: {body.bank_name}, Account: {body.account_number}, Holder: {body.account_holder}",
    )
    
    # Update transaction with banking details
    transaction.bank_name = body.bank_name
    transaction.account_number = body.account_number
    await transaction.save()
    
    logger.info(
        f"Withdrawal requested: {driver.email} | R{body.amount:.2f} | "
        f"Ref: {transaction.reference}"
    )
    
    return {
        "success": True,
        "message": "Withdrawal request submitted. Funds will be transferred within 24-48 hours.",
        "reference": transaction.reference,
        "amount": body.amount,
        "new_balance": driver.wallet_balance,
    }


@router.post("/admin/wallet/adjust")
async def admin_wallet_adjustment(
    body: AdminAdjustment,
    current_user: User = Depends(get_current_user),
):
    """Admin: Manually adjust driver wallet (bonus/penalty/adjustment)"""
    
    # TODO: Add admin check
    
    driver = await DeliveryDriver.get(body.driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    
    # Validate transaction type
    try:
        trans_type = TransactionType(body.type)
        if trans_type not in [TransactionType.BONUS, TransactionType.PENALTY, TransactionType.ADJUSTMENT]:
            raise ValueError()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Type must be: bonus, penalty, or adjustment"
        )
    
    # Create transaction
    transaction = await create_wallet_transaction(
        driver=driver,
        transaction_type=trans_type,
        amount=body.amount,
        description=body.description,
        notes=body.notes,
        processed_by=str(current_user.id),
    )
    
    logger.info(
        f"Admin wallet adjustment: {driver.email} | {body.type} | "
        f"R{body.amount:.2f} by {current_user.email}"
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
    """Get orders ready for pickup (status: ready, no assigned driver)"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    if not driver.is_available:
        raise HTTPException(
            status_code=403,
            detail="Please go online first to see available orders"
        )
    
    # Find orders that are ready for delivery
    orders = await Order.find(
        Order.status == OrderStatus.READY
    ).sort("-created_at").limit(20).to_list()
    
    # Filter out orders already assigned
    assigned_order_ids = {
        a.order_id for a in await DeliveryAssignment.find(
            DeliveryAssignment.status.in_([
                AssignmentStatus.ACCEPTED,
                AssignmentStatus.PICKED_UP,
                AssignmentStatus.IN_TRANSIT,
            ])
        ).to_list()
    }
    
    available = [o for o in orders if str(o.id) not in assigned_order_ids]
    
    # Get customer names
    from models.user import User as UserModel
    result = []
    for order in available:
        customer = await UserModel.get(order.user_id)
        result.append(
            AvailableOrderResponse(
                order_id=str(order.id),
                short_id=str(order.id)[-8:].upper(),
                customer_name=customer.full_name if customer else "Customer",
                delivery_address=order.delivery_address,
                total_amount=order.total_amount,
                delivery_fee=15.0,  # Fixed delivery fee
                distance_km=None,  # TODO: Calculate from address
                created_at=order.created_at,
            )
        )
    
    return result


@router.post("/accept-order")
async def accept_order(
    body: AcceptOrderRequest,
    current_user: User = Depends(get_current_user),
):
    """Driver accepts an order for delivery"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    if not driver.is_available:
        raise HTTPException(status_code=403, detail="You must be online to accept orders")
    
    if driver.current_order_id:
        raise HTTPException(
            status_code=400,
            detail="You already have an active delivery. Complete it first."
        )
    
    # Check if order exists and is ready
    order = await Order.get(body.order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    if order.status != OrderStatus.READY:
        raise HTTPException(
            status_code=400,
            detail=f"Order is not ready for delivery (status: {order.status.value})"
        )
    
    # Check if already assigned
    existing = await DeliveryAssignment.find_one(
        DeliveryAssignment.order_id == body.order_id,
        DeliveryAssignment.status.in_([
            AssignmentStatus.ACCEPTED,
            AssignmentStatus.PICKED_UP,
            AssignmentStatus.IN_TRANSIT,
        ])
    )
    
    if existing:
        raise HTTPException(status_code=409, detail="Order already accepted by another driver")
    
    # Get customer info
    customer = await User.get(order.user_id)
    
    # Create delivery assignment
    assignment = DeliveryAssignment(
        order_id=str(order.id),
        driver_id=str(driver.id),
        driver_name=driver.full_name,
        driver_phone=driver.phone,
        customer_name=customer.full_name if customer else "Customer",
        customer_phone=order.phone or "",
        delivery_address=order.delivery_address,
        status=AssignmentStatus.ACCEPTED,
        delivery_fee=15.0,
        accepted_at=datetime.utcnow(),
    )
    
    await assignment.insert()
    
    # Update driver
    driver.current_order_id = str(order.id)
    driver.updated_at = datetime.utcnow()
    await driver.save()
    
    logger.info(f"Order {order.id} accepted by driver {driver.email}")
    
    return {
        "success": True,
        "message": "Order accepted! Head to the restaurant to pick it up.",
        "assignment_id": str(assignment.id),
        "order_short_id": str(order.id)[-8:].upper(),
        "delivery_address": order.delivery_address,
        "customer_phone": order.phone,
    }


@router.patch("/update-delivery-status")
async def update_delivery_status(
    body: UpdateDeliveryStatus,
    current_user: User = Depends(get_current_user),
):
    """Update delivery status (picked_up, in_transit, delivered)"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    assignment = await DeliveryAssignment.get(body.assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="Delivery assignment not found")
    
    if assignment.driver_id != str(driver.id):
        raise HTTPException(status_code=403, detail="Not your delivery")
    
    # Validate status transition
    try:
        new_status = AssignmentStatus(body.status)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status: {body.status}")
    
    # Update assignment status
    assignment.status = new_status
    assignment.notes = body.notes
    
    now = datetime.utcnow()
    
    if new_status == AssignmentStatus.PICKED_UP:
        assignment.picked_up_at = now
    elif new_status == AssignmentStatus.DELIVERED:
        assignment.delivered_at = now
        
        # Calculate delivery time
        if assignment.accepted_at:
            total_minutes = int((now - assignment.accepted_at).total_seconds() / 60)
            assignment.actual_time = total_minutes
        
        # Update order status
        order = await Order.get(assignment.order_id)
        if order:
            order.status = OrderStatus.DELIVERED
            await order.save()
        
        # Pay driver - add to wallet
        await create_wallet_transaction(
            driver=driver,
            transaction_type=TransactionType.DELIVERY_PAYMENT,
            amount=assignment.delivery_fee,
            description=f"Delivery payment - Order #{assignment.order_id[-8:].upper()}",
            order_id=assignment.order_id,
        )
        
        # Update driver stats
        driver.total_deliveries += 1
        driver.current_order_id = None
        await driver.save()
        
        logger.info(
            f"Delivery completed: {assignment.order_id} by {driver.email} | "
            f"Fee: R{assignment.delivery_fee:.2f} | Time: {assignment.actual_time}min"
        )
    
    await assignment.save()
    
    return {
        "success": True,
        "message": f"Status updated to {new_status.value}",
        "status": new_status.value,
    }


@router.get("/active-delivery")
async def get_active_delivery(current_user: User = Depends(get_current_user)):
    """Get driver's current active delivery"""
    
    driver = await get_driver_by_user(str(current_user.id))
    if not driver:
        raise HTTPException(status_code=404, detail="Driver profile not found")
    
    if not driver.current_order_id:
        return {"active": False, "message": "No active delivery"}
    
    assignment = await DeliveryAssignment.find_one(
        DeliveryAssignment.order_id == driver.current_order_id,
        DeliveryAssignment.driver_id == str(driver.id),
    )
    
    if not assignment:
        # Clear stale current_order_id
        driver.current_order_id = None
        await driver.save()
        return {"active": False, "message": "No active delivery"}
    
    order = await Order.get(assignment.order_id)
    
    return {
        "active": True,
        "assignment_id": str(assignment.id),
        "order_id": assignment.order_id,
        "short_id": assignment.order_id[-8:].upper(),
        "status": assignment.status.value,
        "customer_name": assignment.customer_name,
        "customer_phone": assignment.customer_phone,
        "delivery_address": assignment.delivery_address,
        "delivery_fee": assignment.delivery_fee,
        "total_amount": order.total_amount if order else 0,
        "accepted_at": assignment.accepted_at,
        "picked_up_at": assignment.picked_up_at,
    }


# ── Customer: Rate Driver ──────────────────────────────────────────────────

@router.post("/rate-driver")
async def rate_driver(
    body: RateDriver,
    current_user: User = Depends(get_current_user),
):
    """Customer rates their delivery driver"""
    
    assignment = await DeliveryAssignment.get(body.assignment_id)
    if not assignment:
        raise HTTPException(status_code=404, detail="Delivery not found")
    
    # Verify this is the customer's order
    order = await Order.get(assignment.order_id)
    if not order or order.user_id != str(current_user.id):
        raise HTTPException(status_code=403, detail="Not your order")
    
    if assignment.status != AssignmentStatus.DELIVERED:
        raise HTTPException(
            status_code=400,
            detail="Can only rate completed deliveries"
        )
    
    if assignment.rating is not None:
        raise HTTPException(status_code=400, detail="You already rated this delivery")
    
    # Save rating
    assignment.rating = body.rating
    assignment.rating_comment = body.comment
    await assignment.save()
    
    # Update driver's average rating
    driver = await DeliveryDriver.get(assignment.driver_id)
    if driver:
        total_rating_points = driver.rating * driver.total_ratings
        new_total_ratings = driver.total_ratings + 1
        new_avg_rating = (total_rating_points + body.rating) / new_total_ratings
        
        driver.rating = round(new_avg_rating, 2)
        driver.total_ratings = new_total_ratings
        await driver.save()
        
        logger.info(
            f"Driver {driver.email} rated {body.rating}/5 | "
            f"New avg: {driver.rating} ({driver.total_ratings} ratings)"
        )
    
    return {
        "success": True,
        "message": "Thank you for rating your driver!",
        "rating": body.rating,
    }



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


@router.get("/assignment-by-order/{order_id}")
async def get_assignment_by_order(
    order_id: int,
    current_user: User = Depends(get_current_user)
):
    """Get delivery assignment details for a specific order (for customers)"""
    assignment = await DeliveryAssignment.filter(
        order_id=order_id
    ).select_related("driver").first()
    
    if not assignment:
        raise HTTPException(status_code=404, detail="No delivery found for this order")
    
    return {
        "assignment_id": assignment.id,
        "status": assignment.status,
        "driver": {
            "id": assignment.driver.id,
            "full_name": assignment.driver.full_name,
            "phone": assignment.driver.phone,
            "vehicle_type": assignment.driver.vehicle_type,
        } if assignment.driver else None,
        "picked_up_at": assignment.picked_up_at,
        "delivered_at": assignment.delivered_at,
        "created_at": assignment.created_at,
    }
