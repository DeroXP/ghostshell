"""Benchmark + crash telemetry (Phase H, opt-in).  Stub only in Phase A."""
from fastapi import APIRouter

router = APIRouter(prefix="/v1/telemetry", tags=["telemetry"])


@router.get("/_status")
async def telemetry_status() -> dict:
    return {"phase": "H", "implemented": False,
            "notes": "Telemetry pipeline (benchmark runs + crash reports) lands in Phase H. Opt-in only."}
