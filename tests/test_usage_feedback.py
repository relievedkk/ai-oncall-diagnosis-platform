import hashlib
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def usage_service():
    from sqlalchemy import delete

    from app.db.models import ChatInteraction, User
    from app.services.usage_feedback import UsageFeedbackService

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL required for feedback persistence")
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as s:
        for i in (1, 2):
            if await s.get(User, i) is None:
                s.add(
                    User(
                        id=i,
                        username=f"feedback-test-{i}",
                        display_name="test",
                        password_hash="not-a-real-password",
                        role="operator",
                        is_active=True,
                    )
                )
    yield UsageFeedbackService(sessions)
    async with sessions.begin() as s:
        await s.execute(
            delete(ChatInteraction).where(
                ChatInteraction.session_hash == hashlib.sha256(b"session").hexdigest(),
                ChatInteraction.user_id.in_([1, 2]),
            )
        )
    await engine.dispose()


@pytest.mark.asyncio
async def test_evaluation_data_does_not_become_real_user_feedback(usage_service):
    service = usage_service
    identity = await service.start(1, "session", "evaluation")
    await service.finish(identity, "completed", duration_ms=1000, answer_chars=10)
    await service.feedback(identity, 1, "resolved", "fixture")
    summary = await service.summary()
    assert summary["interactions"] == 0
    assert summary["feedback_count"] == 0
    assert summary["resolved_rate"] is None


@pytest.mark.asyncio
async def test_feedback_ownership_idempotence_and_denominator(usage_service):
    service = usage_service
    identity = await service.start(1, "session", "real")
    await service.finish(
        identity,
        "completed",
        duration_ms=2000,
        answer_chars=10,
        input_tokens=11,
        output_tokens=3,
        model_calls=1,
        usage_complete=True,
    )
    with pytest.raises(PermissionError):
        await service.feedback(identity, 2, "resolved", "")
    await service.feedback(identity, 1, "resolved", "first")
    await service.feedback(identity, 1, "unresolved", "changed")
    result = await service.summary()
    assert result["feedback_count"] == 1
    assert result["resolved_rate"] == 0
    assert result["feedback_coverage"] == 1
    assert result["latency_p95_ms"] == 2000
    assert result["usage"]["input_tokens"] == 11
    assert result["usage"]["coverage"] == 1


def test_feedback_endpoint_requires_human_and_admin_summary():
    from app.api.feedback import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    client = TestClient(app)
    assert (
        client.post(
            "/api/feedback", json={"interaction_id": "x", "resolution": "resolved"}
        ).status_code
        == 401
    )
    assert client.get("/api/feedback/summary").status_code == 401


def test_feedback_payload_cannot_claim_data_origin():
    from pydantic import ValidationError

    from app.models.feedback import FeedbackRequest

    with pytest.raises(ValidationError):
        FeedbackRequest(interaction_id="x", resolution="resolved", data_origin="real")
