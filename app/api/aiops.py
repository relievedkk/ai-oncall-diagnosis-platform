"""
AIOps 智能运维接口
"""

import hashlib
import json

from fastapi import APIRouter, Header, HTTPException, Query, Request
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.config import config
from app.models.aiops import AIOpsRequest, DiagnosisJobCreated
from app.services.alert_diagnosis_service import (
    IdempotencyConflictError,
    alert_diagnosis_service,
    parse_stream_cursor,
)

router = APIRouter()


def _actor(request: Request) -> tuple[str, bool]:
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"user:{principal.user_id}", principal.role == "admin"
    if getattr(request.state, "auth_kind", None) == "api_key":
        # The trusted team machine credential is one principal; never persist the secret.
        return "api-key:" + hashlib.sha256(config.api_key.encode()).hexdigest(), True
    if not config.auth_enabled:
        return "local-development", True
    raise HTTPException(status_code=401, detail="Authentication required")


@router.post("/aiops/jobs", status_code=202, response_model=DiagnosisJobCreated)
async def create_diagnosis_job(
    payload: AIOpsRequest, request: Request,
    idempotency_key: str = Header(min_length=1, max_length=200),
):
    owner, _ = _actor(request)
    try:
        diagnosis_id = await alert_diagnosis_service.enqueue_manual(
            payload.session_id or "default", owner=owner, idempotency_key=idempotency_key,
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"diagnosis_id": diagnosis_id, "events_url": f"/api/aiops/jobs/{diagnosis_id}/events"}


@router.get("/aiops/jobs/{diagnosis_id}/events")
async def diagnosis_job_events(
    diagnosis_id: str, request: Request, after_id: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None),
):
    owner, admin = _actor(request)
    job = await alert_diagnosis_service.repository.get_job(diagnosis_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Diagnosis job not found")
    if job.get("trigger") == "manual":
        creator = job.get("alert", {}).get("owner")
        if (creator is not None and creator != owner) or (creator is None and not admin):
            raise HTTPException(status_code=403, detail="Diagnosis subscription is not permitted")
    try:
        cursor = parse_stream_cursor(after_id if after_id is not None else last_event_id or 0)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def events():
        async for event in alert_diagnosis_service.stream_job(diagnosis_id, after_id=cursor):
            public = dict(event)
            event_id = public.pop("_event_id", None)
            if event_id is not None:
                public["event_id"] = event_id
            yield {"id": event_id, "event": "message", "data": json.dumps(public, ensure_ascii=False)}

    return EventSourceResponse(events())


@router.post("/aiops")
async def diagnose_stream(request: AIOpsRequest):
    """
    AIOps 故障诊断接口（流式 SSE）

    **功能说明：**
    - 自动获取当前系统的活动告警
    - 使用 Plan-Execute-Replan 模式进行智能诊断
    - 流式返回诊断过程和结果

    **SSE 事件类型：**

    1. `status` - 状态更新
       ```json
       {
         "type": "status",
         "stage": "fetching_alerts",
         "message": "正在获取系统告警信息..."
       }
       ```

    2. `plan` - 诊断计划制定完成
       ```json
       {
         "type": "plan",
         "stage": "plan_created",
         "message": "诊断计划已制定，共 6 个步骤",
         "target_alert": {...},
         "plan": ["步骤1: ...", "步骤2: ..."]
       }
       ```

    3. `step_complete` - 步骤执行完成
       ```json
       {
         "type": "step_complete",
         "stage": "step_executed",
         "message": "步骤执行完成 (2/6)",
         "current_step": "查询系统日志",
         "result_preview": "...",
         "remaining_steps": 4
       }
       ```

    4. `report` - 最终诊断报告
       ```json
       {
         "type": "report",
         "stage": "final_report",
         "message": "最终诊断报告已生成",
         "report": "# 故障诊断报告\\n...",
         "evidence": {...}
       }
       ```

    5. `complete` - 诊断完成
       ```json
       {
         "type": "complete",
         "stage": "diagnosis_complete",
         "message": "诊断流程完成",
         "diagnosis": {...}
       }
       ```

    6. `error` - 错误信息
       ```json
       {
         "type": "error",
         "stage": "error",
         "message": "诊断过程发生错误: ..."
       }
       ```

    **使用示例：**
    ```bash
    curl -X POST "http://localhost:9900/api/aiops" \\
      -H "Content-Type: application/json" \\
      -d '{"session_id": "session-123"}' \\
      --no-buffer
    ```

    **前端使用示例：**
    ```javascript
    const eventSource = new EventSource('/api/aiops');

    eventSource.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.type === 'plan') {
        console.log('诊断计划:', data.plan);
      } else if (data.type === 'step_complete') {
        console.log('步骤完成:', data.current_step);
      } else if (data.type === 'report') {
        console.log('最终报告:', data.report);
      } else if (data.type === 'complete') {
        console.log('诊断完成');
        eventSource.close();
      }
    };
    ```

    Args:
        request: AIOps 诊断请求

    Returns:
        SSE 事件流
    """
    session_id = request.session_id or "default"
    logger.info(f"[会话 {session_id}] 收到 AIOps 诊断请求（流式）")

    async def event_generator():
        try:
            async for event in alert_diagnosis_service.diagnose_manual(session_id=session_id):
                # 发送事件
                yield {"event": "message", "data": json.dumps(event, ensure_ascii=False)}

                # 如果是完成或错误事件，结束流
                if event.get("type") in ["complete", "error"]:
                    break

            logger.info(f"[会话 {session_id}] AIOps 诊断流式响应完成")

        except Exception as e:
            logger.error(f"[会话 {session_id}] AIOps 诊断流式响应异常: {e}", exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps(
                    {"type": "error", "stage": "exception", "message": "诊断服务暂时不可用"},
                    ensure_ascii=False,
                ),
            }

    return EventSourceResponse(event_generator())
