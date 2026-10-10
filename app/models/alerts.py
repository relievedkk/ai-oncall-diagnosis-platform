"""Models for Alertmanager webhook payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_ALERTS_PER_WEBHOOK = 100
MAX_LABELS_PER_ALERT = 100
MAX_METADATA_LENGTH = 2_048


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: str = Field(default="", alias="startsAt")
    ends_at: str = Field(default="", alias="endsAt")
    generator_url: str = Field(default="", alias="generatorURL")
    fingerprint: str = ""

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
