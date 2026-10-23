"""Minimal interaction metadata; feedback is never synthesized from model output."""

import asyncio
import hashlib
import math
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from loguru import logger
from sqlalchemy import func, select

from app.config import config
from app.core.model_protection import UsageCapture, capture_usage
from app.db.models import AnswerFeedback, ChatInteraction
from app.db.session import database_manager


class UsageFeedbackService:
    def __init__(self, sessions):
        self.sessions = sessions

    async def start(self, user_id, session_id, origin):
        if origin not in {"real", "evaluation"}:
            raise ValueError("invalid origin")
        identity = str(uuid4())
        async with self.sessions.begin() as session:
            session.add(
                ChatInteraction(
                    id=identity,
                    user_id=user_id,
                    session_hash=hashlib.sha256(session_id.encode()).hexdigest(),
                    data_origin=origin,
                    status="started",
                )
            )
        return identity

    async def finish(
        self,
        identity,
        status,
        *,
        duration_ms,
        answer_chars=0,
        first_content_ms=None,
        input_tokens=None,
        output_tokens=None,
        model_calls=0,
        usage_complete=False,
    ):
        async with self.sessions.begin() as session:
            row = await session.get(ChatInteraction, identity, with_for_update=True)
            if row is None or row.status != "started":
                return
            row.status, row.duration_ms, row.answer_chars = status, duration_ms, answer_chars
            row.first_content_ms = first_content_ms
            row.input_tokens, row.output_tokens = input_tokens, output_tokens
            row.model_calls, row.usage_complete = model_calls, usage_complete

    async def feedback(self, identity, user_id, resolution, comment):
        if resolution not in {"resolved", "partial", "unresolved"} or len(comment) > 1000:
            raise ValueError("invalid feedback")
        async with self.sessions.begin() as session:
            interaction = await session.get(ChatInteraction, identity, with_for_update=True)
            if interaction is None or interaction.user_id != user_id:
                raise PermissionError("只能评价自己的回答")
            if interaction.status != "completed":
                raise ValueError("只能评价已完成的回答")
            row = await session.get(AnswerFeedback, identity)
            if row is None:
                row = AnswerFeedback(interaction_id=identity, user_id=user_id)
                session.add(row)
            row.resolution, row.comment = resolution, comment
            return {"interaction_id": identity, "resolution": resolution}

    async def summary(self, days=30):
        cutoff = datetime.now(UTC) - timedelta(days=days)
        where = (ChatInteraction.data_origin == "real", ChatInteraction.created_at >= cutoff)
        async with self.sessions() as session:
            counts = dict(
                (
                    await session.execute(
                        select(ChatInteraction.status, func.count())
                        .where(*where)
                        .group_by(ChatInteraction.status)
                    )
                ).all()
            )
            people = await session.scalar(
                select(func.count(func.distinct(ChatInteraction.user_id))).where(*where)
            )
            eligible = await session.scalar(
                select(func.count())
                .select_from(ChatInteraction)
                .where(
                    *where,
                    ChatInteraction.status == "completed",
                    ChatInteraction.user_id.is_not(None),
                )
            )
            feedback_counts = dict(
                (
                    await session.execute(
                        select(AnswerFeedback.resolution, func.count())
                        .join(ChatInteraction, ChatInteraction.id == AnswerFeedback.interaction_id)
                        .where(*where)
                        .group_by(AnswerFeedback.resolution)
                    )
                ).all()
            )
            duration_where = (
                *where,
                ChatInteraction.status == "completed",
                ChatInteraction.duration_ms.is_not(None),
            )
            duration_count = await session.scalar(
                select(func.count()).select_from(ChatInteraction).where(*duration_where)
            )
            called = await session.scalar(
                select(func.count())
                .select_from(ChatInteraction)
                .where(*where, ChatInteraction.model_calls > 0)
            )
            observed, input_tokens, output_tokens = (
                await session.execute(
                    select(
                        func.count(),
                        func.sum(ChatInteraction.input_tokens),
                        func.sum(ChatInteraction.output_tokens),
                    ).where(
                        *where,
                        ChatInteraction.model_calls > 0,
                        ChatInteraction.usage_complete.is_(True),
                    )
                )
            ).one()
            p95 = (
                await session.scalar(
                    select(ChatInteraction.duration_ms)
                    .where(*duration_where)
                    .order_by(ChatInteraction.duration_ms)
                    .offset(max(0, math.ceil(duration_count * 0.95) - 1))
                    .limit(1)
                )
                if duration_count
                else None
            )
        total_feedback = sum(feedback_counts.values())
        return {
            "data_origin": "real",
            "window_days": days,
            "interactions": sum(counts.values()),
            "distinct_human_users": people,
            "statuses": counts,
            "eligible_completed_interactions": eligible,
            "feedback_count": total_feedback,
            "resolutions": feedback_counts,
            "resolved_rate": (
                feedback_counts.get("resolved", 0) / total_feedback if total_feedback else None
            ),
            "feedback_coverage": total_feedback / eligible if eligible else None,
            "latency_p95_ms": p95,
            "usage": {
                "recorded_interactions": observed,
                "model_interactions": called,
                "coverage": observed / called if called else None,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "boundary": "已记录调用用量，不含所有失败重试，不等于供应商账单",
            },
            "interpretation": "用户主动反馈样本中的解决比例；无反馈不算已解决，测试数据排除",
        }


usage_feedback_service = UsageFeedbackService(database_manager.session_factory)


class InteractionTracker:
    def __init__(self, request, session_id):
        self.request, self.session_id = request, session_id
        self.identity = None
        self.started = time.perf_counter()
        self.status = "cancelled"
        self.answer_chars = 0
        self.first_content_ms = None
        self.usage = UsageCapture()
        self.capture = capture_usage(self.usage)
        self.persisted = False

    async def __aenter__(self):
        self.capture.__enter__()
        if config.usage_tracking_enabled and getattr(
            self.request.app.state, "usage_tracking_started", False
        ):
            user = getattr(self.request.state, "principal", None)
            try:
                self.identity = await asyncio.wait_for(
                    usage_feedback_service.start(
                        getattr(user, "user_id", None), self.session_id, config.usage_data_origin
                    ),
                    3,
                )
            except Exception as exc:
                logger.warning("使用记录暂不可用: {}", type(exc).__name__)
        return self

    def content(self, text):
        if text:
            if self.first_content_ms is None:
                self.first_content_ms = int((time.perf_counter() - self.started) * 1000)
            self.answer_chars += len(text)

    async def __aexit__(self, kind, value, traceback):
        try:
            if kind is not None and not issubclass(kind, (asyncio.CancelledError, GeneratorExit)):
                self.status = "failed"
            await self.persist()
        finally:
            self.capture.__exit__(kind, value, traceback)

    async def persist(self):
        if self.identity and not self.persisted:
            self.persisted = True
            complete = self.usage.calls == self.usage.calls_with_usage
            try:
                await asyncio.wait_for(
                    asyncio.shield(
                        usage_feedback_service.finish(
                            self.identity,
                            self.status,
                            duration_ms=int((time.perf_counter() - self.started) * 1000),
                            answer_chars=self.answer_chars,
                            first_content_ms=self.first_content_ms,
                            input_tokens=self.usage.input_tokens if complete else None,
                            output_tokens=self.usage.output_tokens if complete else None,
                            model_calls=self.usage.calls,
                            usage_complete=complete,
                        )
                    ),
                    3,
                )
            except Exception as exc:
                logger.warning("使用记录保存失败: {}", type(exc).__name__)
                self.identity = None
