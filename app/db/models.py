"""SQLAlchemy models for durable diagnosis history."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class DiagnosisJob(Base):
    __tablename__ = "diagnosis_jobs"

    diagnosis_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    fingerprint: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    alert_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    service: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    alert_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    severity: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    alert_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[DiagnosisStep]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    evidence: Mapped[list[DiagnosisEvidence]] = relationship(
        back_populates="job", cascade="all, delete-orphan", passive_deletes=True
    )
    report: Mapped[DiagnosisReport | None] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )

    __table_args__ = (Index("ix_diagnosis_jobs_created_at", "created_at"),)


class DiagnosisStep(Base):
    __tablename__ = "diagnosis_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    diagnosis_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_jobs.diagnosis_id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    job: Mapped[DiagnosisJob] = relationship(back_populates="steps")
    evidence: Mapped[list[DiagnosisEvidence]] = relationship(back_populates="step")

    __table_args__ = (
        UniqueConstraint("diagnosis_id", "sequence", name="uq_diagnosis_step_sequence"),
    )


class DiagnosisEvidence(Base):
    __tablename__ = "diagnosis_evidence"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    diagnosis_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_jobs.diagnosis_id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnosis_steps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    evidence_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )

    job: Mapped[DiagnosisJob] = relationship(back_populates="evidence")
    step: Mapped[DiagnosisStep | None] = relationship(back_populates="evidence")


class DiagnosisReport(Base):
    __tablename__ = "diagnosis_reports"

    diagnosis_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_jobs.diagnosis_id", ondelete="CASCADE"), primary_key=True
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False, default="markdown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[DiagnosisJob] = relationship(back_populates="report")
