"""Alertmanager webhook and diagnosis job APIs."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Header, HTTPException, Response, status

from app.config import config
from app.models.alerts import AlertmanagerWebhook
from app.services.alert_diagnosis_service import alert_diagnosis_service

router = APIRouter()


def _verify_alertmanager_token(authorization: str | None) -> None:
    expected = config.alertmanager_webhook_token
    if not expected:
        return
    supplied = authorization or ""
    prefix = "Bearer "
    if not supplied.startswith(prefix) or not secrets.compare_digest(supplied[len(prefix):], expected):
        raise HTTPException(status_code=401, detail="Invalid Alertmanager webhook token")


@router.post("/alerts/webhook", status_code=status.HTTP_202_ACCEPTED)
async def alertmanager_webhook(
    payload: AlertmanagerWebhook,
    authorization: str | None = Header(default=None),
):
    _verify_alertmanager_token(authorization)
    diagnosis_ids = await alert_diagnosis_service.handle_webhook(payload)
    return {
        "accepted": True,
        "received_alerts": len(payload.alerts),
        "diagnosis_ids": diagnosis_ids,
    }


@router.get("/aiops/jobs")
async def list_aiops_jobs():
    return {"jobs": await alert_diagnosis_service.list_jobs()}


@router.get("/aiops/jobs/{diagnosis_id}")
async def get_aiops_job(diagnosis_id: str):
    job = await alert_diagnosis_service.get_job(diagnosis_id)
    if not job:
        raise HTTPException(status_code=404, detail="Diagnosis job not found")
    return job


@router.get("/alerts/webhook", include_in_schema=False)
async def alertmanager_webhook_probe():
    return Response(status_code=405)
