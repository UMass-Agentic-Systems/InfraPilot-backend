from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ChatRole = Literal["user", "infra-agent", "sre-agent"]
AgentName = Literal["infra-agent", "sre-agent"]


# ---------------------------------------------------------------------------
# REST payloads
# ---------------------------------------------------------------------------


class SessionCreate(BaseModel):
    title: str = Field(default="New Chat", max_length=255)


class SessionSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    role: ChatRole
    content: str
    metadata_json: str | None = None
    position: int
    created_at: datetime


class SessionDetail(SessionSummary):
    messages: list[MessageOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# WebSocket frame models (spec §6.4)
# ---------------------------------------------------------------------------


class WSClientMessage(BaseModel):
    type: Literal["message"] = "message"
    content: str = Field(min_length=1)


class WSAgentResponse(BaseModel):
    type: Literal["agent_response"] = "agent_response"
    message: MessageOut


class WSSREAlert(BaseModel):
    type: Literal["sre_alert"] = "sre_alert"
    deployment_id: int
    app_name: str
    message: MessageOut


class WSTyping(BaseModel):
    type: Literal["typing"] = "typing"
    agent: AgentName
    is_typing: bool


class WSError(BaseModel):
    type: Literal["error"] = "error"
    detail: str
