from fastapi import APIRouter, HTTPException, Query, Request

from app.models.feedback import FeedbackRequest
from app.services.usage_feedback import usage_feedback_service

router = APIRouter()


def human(request):
    principal = getattr(request.state, "principal", None)
    if principal is None or getattr(request.state, "auth_kind", None) != "session":
        raise HTTPException(401, "请登录后提交反馈")
    return principal


@router.post("/feedback")
async def submit_feedback(payload: FeedbackRequest, request: Request):
    user = human(request)
    try:
        return await usage_feedback_service.feedback(
            payload.interaction_id, user.user_id, payload.resolution, payload.comment
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("/feedback/summary")
async def feedback_summary(request: Request, days: int = Query(default=30, ge=1, le=365)):
    user = human(request)
    if user.role != "admin":
        raise HTTPException(403, "仅管理员可查看汇总")
    return await usage_feedback_service.summary(days)
