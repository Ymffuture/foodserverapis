# routes/addresses.py
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from typing import Optional, List
from models.user import User
from models.saved_address import SavedAddress
from dependencies import get_current_user

router = APIRouter(prefix="/addresses", tags=["Addresses"])

MAX_SAVED_ADDRESSES = 10


class AddressCreate(BaseModel):
    label: str = "Home"
    address: str
    phone: Optional[str] = None
    is_default: bool = False

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("Label can't be empty.")
        return v[:20]

    @field_validator("address")
    @classmethod
    def _validate_address(cls, v):
        v = (v or "").strip()
        if len(v) < 5:
            raise ValueError("Address is too short.")
        return v


class AddressUpdate(BaseModel):
    label: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    is_default: Optional[bool] = None


def _serialize(a: SavedAddress) -> dict:
    return {
        "id": str(a.id),
        "label": a.label,
        "address": a.address,
        "phone": a.phone,
        "is_default": a.is_default,
        "created_at": a.created_at,
    }


async def _unset_other_defaults(user_id: str, except_id: Optional[str] = None):
    others = await SavedAddress.find({"user_id": user_id, "is_default": True}).to_list()
    for o in others:
        if except_id and str(o.id) == except_id:
            continue
        o.is_default = False
        await o.save()


@router.get("/me")
async def list_my_addresses(current_user: User = Depends(get_current_user)) -> List[dict]:
    addresses = await SavedAddress.find({"user_id": str(current_user.id)}).to_list()
    addresses.sort(key=lambda a: (not a.is_default, a.created_at))
    return [_serialize(a) for a in addresses]


@router.post("/", status_code=201)
async def create_address(body: AddressCreate, current_user: User = Depends(get_current_user)):
    count = await SavedAddress.find({"user_id": str(current_user.id)}).count()
    if count >= MAX_SAVED_ADDRESSES:
        raise HTTPException(status_code=422, detail=f"You can save up to {MAX_SAVED_ADDRESSES} addresses.")

    # First address a user saves is automatically their default.
    is_default = body.is_default or count == 0

    address = SavedAddress(
        user_id=str(current_user.id),
        label=body.label,
        address=body.address,
        phone=body.phone,
        is_default=is_default,
    )
    await address.insert()

    if is_default:
        await _unset_other_defaults(str(current_user.id), except_id=str(address.id))

    return _serialize(address)


@router.patch("/{address_id}")
async def update_address(address_id: str, body: AddressUpdate, current_user: User = Depends(get_current_user)):
    address = await SavedAddress.get(address_id)
    if not address or address.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Address not found.")

    if body.label is not None:
        address.label = body.label.strip()[:20] or address.label
    if body.address is not None and len(body.address.strip()) >= 5:
        address.address = body.address.strip()
    if body.phone is not None:
        address.phone = body.phone.strip() or None
    if body.is_default is True:
        address.is_default = True

    await address.save()

    if address.is_default:
        await _unset_other_defaults(str(current_user.id), except_id=str(address.id))

    return _serialize(address)


@router.delete("/{address_id}")
async def delete_address(address_id: str, current_user: User = Depends(get_current_user)):
    address = await SavedAddress.get(address_id)
    if not address or address.user_id != str(current_user.id):
        raise HTTPException(status_code=404, detail="Address not found.")

    was_default = address.is_default
    await address.delete()

    if was_default:
        remaining = await SavedAddress.find({"user_id": str(current_user.id)}).to_list()
        if remaining:
            remaining.sort(key=lambda a: a.created_at)
            remaining[0].is_default = True
            await remaining[0].save()

    return {"deleted": True}
