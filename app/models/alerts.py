"""Models for Alertmanager webhook payloads."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_ALERTS_PER_WEBHOOK = 100
MAX_LABELS_PER_ALERT = 100
MAX_METADATA_LENGTH = 2_048
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?P<fraction>\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$")


def parse_alert_timestamp(value: str) -> datetime:
    """Require an aware RFC3339 instant and compare it in UTC."""
    if not isinstance(value, str) or len(value) > 64 or not _RFC3339.fullmatch(value):
        raise ValueError("alert timestamp must be an aware RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("alert timestamp is outside supported UTC range") from exc


def canonical_alert_timestamp(value: str) -> str:
    parsed = parse_alert_timestamp(value)
    fraction = (_RFC3339.fullmatch(value).group("fraction") or "")[1:].rstrip("0")
    # datetime stores only microseconds; preserve Alertmanager's nanoseconds.
    seconds = parsed.replace(microsecond=0).isoformat()
    return seconds[:-6] + (f".{fraction}" if fraction else "") + "+00:00"


def _timestamp_order(value: str) -> tuple[datetime, str]:
    parsed = parse_alert_timestamp(value)
    fraction = (_RFC3339.fullmatch(value).group("fraction") or "")[1:].ljust(9, "0")
    return parsed, fraction[6:]


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: str = Field(alias="startsAt", max_length=64)
    ends_at: str = Field(default="", alias="endsAt", max_length=64)
    generator_url: str = Field(default="", alias="generatorURL")
    fingerprint: str = Field(default="", max_length=255)

    @field_validator("labels")
    @classmethod
    def validate_persisted_labels(cls, value: dict[str, str]) -> dict[str, str]:
        limits = {"alertname": 255, "severity": 64}
        # Catalog fallback persists the first nonempty service identity label.
        fallback = next((name for name in ("service", "job", "app", "instance") if value.get(name)), None)
        if fallback:
            limits[fallback] = 255
        if any(len(value.get(name, "")) > limit for name, limit in limits.items()):
            raise ValueError("alert identity label exceeds storage limits")
        return value

    @model_validator(mode="after")
    def validate_timestamps(self) -> AlertmanagerAlert:
        start = _timestamp_order(self.starts_at)
        end = _timestamp_order(self.ends_at) if self.ends_at else None
        zero_end = end == (datetime.min.replace(tzinfo=UTC), "000")
        if self.status == "resolved" and (end is None or zero_end or end < start):
            raise ValueError("resolved alert endsAt must be at or after startsAt")
        if end is not None and not zero_end and end < start:
            raise ValueError("alert endsAt precedes startsAt")
        return self

    @field_validator("labels", "annotations")
    @classmethod
    def validate_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_LABELS_PER_ALERT:
            raise ValueError("too many alert labels or annotations")
        if any(
            len(str(key)) > 128 or len(str(item)) > MAX_METADATA_LENGTH
            for key, item in value.items()
        ):
            raise ValueError("alert metadata is too large")
        return value


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    receiver: str = ""
    status: str = ""
    alerts: list[AlertmanagerAlert] = Field(default_factory=list, max_length=MAX_ALERTS_PER_WEBHOOK)
    group_labels: dict[str, str] = Field(default_factory=dict, alias="groupLabels")
    common_labels: dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    common_annotations: dict[str, str] = Field(default_factory=dict, alias="commonAnnotations")
    external_url: str = Field(default="", alias="externalURL")
    version: str = ""
    group_key: str = Field(default="", alias="groupKey")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)
