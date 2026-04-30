"""SRE monitor REST endpoints (spec §6.3, issue #13).

Standalone HTTP surface for triggering scans, listing remediation plans, and
approving/rejecting them. The chat/WebSocket flow shares the same SRE graph
via deployment-scoped thread IDs, so plans created here can be approved via
chat and vice versa (spec §6.4).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.sre.graph import resume_sre_agent, run_sre_agent
from app.api.deps import get_current_user, get_db
from app.api.schemas.monitor import (
    ApproveRequest,
    ApproveResponse,
    PlanSummary,
    ScanRequest,
    ScanResponse,
)
from app.db.models import Deployment, RemediationPlan, User

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/scan", response_model=ScanResponse)
async def scan(
    payload: ScanRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ScanResponse:
    """Run the SRE graph to the approval interrupt for a user-owned deployment.

    Cross-user / unknown deployment ids collapse to 404 per spec §11.4.
    """
    deployment = db.get(Deployment, payload.deployment_id)
    if deployment is None or deployment.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    result = await run_sre_agent(
        user_id=user.id,
        namespace=user.namespace,
        deployment_id=deployment.id,
        source="manual",
        db=db,
    )
    return ScanResponse(**result)


@router.get("/plans", response_model=list[PlanSummary])
def list_plans(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RemediationPlan]:
    """All remediation plans across the caller's deployments, newest first."""
    return (
        db.query(RemediationPlan)
        .join(Deployment, RemediationPlan.deployment_id == Deployment.id)
        .filter(Deployment.user_id == user.id)
        .order_by(RemediationPlan.created_at.desc())
        .all()
    )


@router.post("/plans/{plan_id}/approve", response_model=ApproveResponse)
async def approve_plan(
    plan_id: int,
    payload: ApproveRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ApproveResponse:
    """Resume the paused SRE graph with the user's decision.

    Spec §11.4 / issue #13: cross-user plan access returns 403 here (distinct
    from the 404 used on deployment lookups), since the plan id is known to
    exist but is not owned by the caller.
    """
    plan = db.get(RemediationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    deployment = db.get(Deployment, plan.deployment_id)
    if deployment is None or deployment.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    result = await resume_sre_agent(
        user_id=user.id,
        deployment_id=plan.deployment_id,
        approved=payload.approved,
        db=db,
    )
    return ApproveResponse(**result)
