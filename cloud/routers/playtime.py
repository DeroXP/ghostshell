"""Playtime ledger (Phase B/D).  Stub only in Phase A."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1/playtime", tags=["playtime"])


@router.get("/_status")
async def playtime_status() -> dict:
    return {"phase": "B", "implemented": False,
            "notes": "Per-game playtime tracking lands in Phase B/D."}
