import json

import pytest

from app.services.alert_diagnosis_service import AlertDiagnosisService, _format_stream_evidence


def test_preexisting_stream_events_are_redacted_on_read():
    event = _format_stream_evidence({"payload": {"type": "status", "message": "Authorization: Bearer SYNTHETIC_LEGACY_STREAM_SECRET"}})
    assert "SYNTHETIC_LEGACY_STREAM_SECRET" not in json.dumps(event)


@pytest.mark.asyncio
async def test_preexisting_job_detail_does_not_expose_raw_credential_fields():
    class Repository:
        async def get_job(self, _):
            return {"diagnosis_id": "legacy-test", "report": "password=SYNTHETIC_LEGACY_REPORT_SECRET",
                    "evidence": [{"payload": {"Cookie": "session=SYNTHETIC_LEGACY_COOKIE_SECRET"}}]}

    service = AlertDiagnosisService(Repository(), inline_worker=False)
    returned = await service.get_job("legacy-test")
    serialized = json.dumps(returned)
    assert "SYNTHETIC_LEGACY_REPORT_SECRET" not in serialized
    assert "SYNTHETIC_LEGACY_COOKIE_SECRET" not in serialized
    assert returned["diagnosis_id"] == "legacy-test"
