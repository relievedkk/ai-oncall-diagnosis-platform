"""The in-memory adapter enforces the same proposal ownership boundary as SQL."""

import pytest

from app.repositories.diagnosis import DiagnosisLeaseLostError, InMemoryDiagnosisRepository
from app.repositories.remediation import InMemoryRemediationRepository


@pytest.mark.asyncio
async def test_proposal_with_execution_identity_requires_ownership_repository():
    repository = InMemoryRemediationRepository()
    with pytest.raises(DiagnosisLeaseLostError):
        await repository.create({"diagnosis_id": "job"}, False, execution_token="unknown")
    assert await repository.count_active() == 0


@pytest.mark.asyncio
async def test_proposal_rejects_stale_identity_and_accepts_current_identity():
    diagnoses = InMemoryDiagnosisRepository()
    await diagnoses.create_or_get_job({"diagnosis_id": "job", "event_key": "job", "status": "queued"})
    await diagnoses.claim_job("job", "task", "worker", execution_token="current")
    repository = InMemoryRemediationRepository(diagnoses)
    values = {"id": "action", "diagnosis_id": "job", "idempotency_key": "job:action", "status": "proposed"}
    with pytest.raises(DiagnosisLeaseLostError):
        await repository.create(values, False, execution_token="old")
    assert await repository.count_for_diagnosis("job") == 0
    action, created = await repository.create(values, False, execution_token="current")
    assert created and action["id"] == "action"
    assert await repository.count_for_diagnosis("job") == 1
