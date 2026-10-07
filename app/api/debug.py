"""Explicitly gated local fault injection for end-to-end monitoring tests."""

from fastapi import APIRouter, HTTPException

from app.config import config
from app.core.metrics import SYNTHETIC_FAULT

router = APIRouter()


@router.post("/debug/fault/{state}")
async def set_synthetic_fault(state: str):
    if not config.fault_injection_enabled:
        raise HTTPException(status_code=404, detail="Fault injection is disabled")
    if state not in {"active", "resolved"}:
        raise HTTPException(status_code=422, detail="state must be active or resolved")

    value = 1 if state == "active" else 0
    SYNTHETIC_FAULT.labels(service="superbiz-agent", fault="e2e").set(value)
    return {"service": "superbiz-agent", "fault": "e2e", "state": state, "value": value}
