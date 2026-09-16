"""Minimal Prometheus-compatible process metrics endpoint."""

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Expose default process and Python runtime metrics to Prometheus."""
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    except ImportError:
        return Response(
            "# prometheus-client is not installed\n",
            media_type="text/plain; version=0.0.4",
            status_code=503,
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
