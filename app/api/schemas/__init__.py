"""Shared Pydantic schemas for API request/response payloads.

Submodules are also importable directly, e.g.
``from app.api.schemas.auth import RegisterRequest``. The re-exports below
exist as a convenience for routers that consume schemas from multiple
domains.
"""

from app.api.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.api.schemas.chat import (
    AgentName,
    ChatRole,
    MessageOut,
    SessionCreate,
    SessionDetail,
    SessionSummary,
    WSAgentResponse,
    WSClientMessage,
    WSError,
    WSSREAlert,
    WSTyping,
)
from app.api.schemas.deploy import DeploymentDetail, DeployRequest, DeployResponse
from app.api.schemas.monitor import (
    ApproveRequest,
    ApproveResponse,
    PlanSummary,
    ScanRequest,
    ScanResponse,
)
from app.api.schemas.visualize import (
    ClusterInfo,
    ContainerInfo,
    HPAInfo,
    NodeStatus,
    PodCounts,
    RemediationPlanRef,
    ResourceInfo,
    ServiceInfo,
    StorageInfo,
    TierInfo,
    TrafficMetrics,
    VisualizeResponse,
)

__all__ = [
    # auth
    "LoginRequest",
    "RegisterRequest",
    "TokenResponse",
    # chat
    "AgentName",
    "ChatRole",
    "MessageOut",
    "SessionCreate",
    "SessionDetail",
    "SessionSummary",
    "WSAgentResponse",
    "WSClientMessage",
    "WSError",
    "WSSREAlert",
    "WSTyping",
    # deploy
    "DeploymentDetail",
    "DeployRequest",
    "DeployResponse",
    # monitor
    "ApproveRequest",
    "ApproveResponse",
    "PlanSummary",
    "ScanRequest",
    "ScanResponse",
    # visualize
    "ClusterInfo",
    "ContainerInfo",
    "HPAInfo",
    "NodeStatus",
    "PodCounts",
    "RemediationPlanRef",
    "ResourceInfo",
    "ServiceInfo",
    "StorageInfo",
    "TierInfo",
    "TrafficMetrics",
    "VisualizeResponse",
]
