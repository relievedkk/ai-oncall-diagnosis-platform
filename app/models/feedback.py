from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_id: str = Field(min_length=1, max_length=36)
    resolution: Literal["resolved", "partial", "unresolved"]
    comment: str = Field(default="", max_length=1000)
