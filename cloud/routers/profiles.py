"""Per-game profiles (Phase B/D).  Stubs only in Phase A."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1/profiles", tags=["profiles"])


@router.get("/_status")
async def profiles_status() -> dict:
    return {"phase": "B", "implemented": False,
            "notes": "Per-game profile sync lands when Phase B/D ships."}
