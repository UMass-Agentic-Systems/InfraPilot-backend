import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.agents.provisioning.graph import run_provisioning_agent
from app.api.deps import get_current_user, get_db
from app.api.schemas.deploy import DeploymentDetail, DeployRequest, DeployResponse
from app.db.models import Deployment, User

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/", response_model=DeployResponse)
async def deploy(
    payload: DeployRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeployResponse:
    """Standalone deploy endpoint (no chat context).

    Spec §6.2 / A1 fix: agent failure must surface as HTTP 200 with
    `status="failed"` and a populated `error`. The 404 status is reserved
    for the GET path; this endpoint never 404s.
    """
    try:
        result = await run_provisioning_agent(
            user_id=user.id,
            namespace=user.namespace,
            app_name=payload.app_name,
            requirements=payload.requirements,
            chat_session_id=None,
            db=db,
        )
        return DeployResponse(**result)
    except Exception as exc:
        logger.warning(
            "provisioning agent failed for user=%s app=%s: %s",
            user.id,
            payload.app_name,
            exc,
        )
        return DeployResponse(deployment_id=None, status="failed", error=str(exc))


@router.get("/{deployment_id}", response_model=DeploymentDetail)
def get_deployment(
    deployment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Deployment:
    """Return the deployment if owned by the calling user; 404 otherwise.

    Cross-user reads collapse to 404 (not 403) per spec §11.4 — no
    information leakage about which IDs exist.
    """
    deployment = db.get(Deployment, deployment_id)
    if deployment is None or deployment.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")
    return deployment
