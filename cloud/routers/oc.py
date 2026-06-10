"""OC recommendations (Phase H — stickiness algorithm).  Stub only in Phase A."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1/oc", tags=["oc"])


@router.get("/_status")
async def oc_status() -> dict:
    return {"phase": "H", "implemented": False,
            "notes": "Crowd-sourced recommendations land in Phase H."}
