from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ScanRequest(BaseModel):
    deployment_id: int


class ScanResponse(BaseModel):
    plan_db_id: int | None = None
    analysis: str | None = None
    rationale: str | None = None
    status: str  # "awaiting_approval" | "no_issues" | "failed"


class PlanSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    deployment_id: int
    analysis: str
    rationale: str
    approved: bool
    applied: bool
    source: str  # "manual" | "background"
    created_at: datetime


class ApproveRequest(BaseModel):
    approved: bool


class ApproveResponse(BaseModel):
    plan_id: int
    approved: bool
    applied: bool
