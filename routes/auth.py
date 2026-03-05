from fastapi import APIRouter

router = APIRouter(prefix="/auth")

@router.get("/health")
def health():
    return {"status": "ok"}
