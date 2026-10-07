"""Models for Alertmanager webhook payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: str = Field(default="", alias="startsAt")
    ends_at: str = Field(default="", alias="endsAt")
    generator_url: str = Field(default="", alias="generatorURL")
    fingerprint: str = ""


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    receiver: str = ""
    status: str = ""
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)
    group_labels: dict[str, str] = Field(default_factory=dict, alias="groupLabels")
    common_labels: dict[str, str] = Field(default_factory=dict, alias="commonLabels")
    common_annotations: dict[str, str] = Field(default_factory=dict, alias="commonAnnotations")
    external_url: str = Field(default="", alias="externalURL")
    version: str = ""
    group_key: str = Field(default="", alias="groupKey")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True)
