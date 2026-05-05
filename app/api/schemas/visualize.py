from datetime import datetime

from pydantic import BaseModel, Field


class NodeStatus(BaseModel):
    ready: int
    total: int


class ClusterInfo(BaseModel):
    name: str
    provider: str
    region: str
    k8s_version: str
    nodes: NodeStatus


class ContainerInfo(BaseModel):
    name: str
    image: str


class PodCounts(BaseModel):
    running: int = 0
    pending: int = 0
    failed: int = 0
    total: int = 0


class ResourceInfo(BaseModel):
    cpu_usage_percent: float | None = None
    memory_usage_percent: float | None = None
    cpu_requests: str | None = None
    memory_requests: str | None = None


class ServiceInfo(BaseModel):
    type: str | None = None
    port: int | None = None


class HPAInfo(BaseModel):
    min_replicas: int
    max_replicas: int
    cpu_target_percent: int


class StorageInfo(BaseModel):
    used_gi: float
    capacity_gi: float
    storage_class: str


class TierInfo(BaseModel):
    name: str
    kind: str  # "Deployment" | "StatefulSet"
    containers: list[ContainerInfo] = Field(default_factory=list)
    pods: PodCounts = Field(default_factory=PodCounts)
    resources: ResourceInfo = Field(default_factory=ResourceInfo)
    service: ServiceInfo | None = None
    hpa: HPAInfo | None = None
    storage: StorageInfo | None = None


class TrafficMetrics(BaseModel):
    # All fields are optional so partial telemetry sources (e.g. pod readiness
    # → uptime only) can populate just what they have. Frontend hides any
    # field that comes back null. Spec FR-14.2.
    requests_per_second: float | None = None
    avg_latency_ms: float | None = None
    p95_latency_ms: float | None = None
    p99_latency_ms: float | None = None
    error_rate_percent: float | None = None
    uptime_percent: float | None = None


class RemediationPlanRef(BaseModel):
    id: int
    analysis: str
    approved: bool
    applied: bool
    rejected: bool
    source: str
    created_at: datetime


class VisualizeResponse(BaseModel):
    deployment_id: int
    app_name: str
    status: str
    namespace: str
    created_at: datetime
    updated_at: datetime
    cluster: ClusterInfo | None = None
    tiers: list[TierInfo] = Field(default_factory=list)
    traffic: TrafficMetrics | None = None
    remediation_plans: list[RemediationPlanRef] = Field(default_factory=list)
    error: str | None = None  # populated if K8s is unreachable (spec §6.5 fallback)
