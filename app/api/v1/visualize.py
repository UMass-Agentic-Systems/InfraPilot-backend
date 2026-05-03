"""Visualization endpoint — live cluster state (spec §6.5, issue #18)."""

import logging
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db
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
    VisualizeResponse,
)
from app.db.models import Deployment, RemediationPlan, User
from app.services.k8s_client import KubernetesService

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# NamespaceStatus → schema helpers
# ---------------------------------------------------------------------------


def _build_cluster(data: dict[str, Any]) -> ClusterInfo:
    nodes = data["nodes"]
    return ClusterInfo(
        name=str(data["name"]),
        provider=str(data["provider"]),
        region=str(data["region"]),
        k8s_version=str(data["k8s_version"]),
        nodes=NodeStatus(ready=int(nodes["ready"]), total=int(nodes["total"])),
    )


def _build_tier(data: dict[str, Any]) -> TierInfo:
    service_data = data.get("service")
    hpa_data = data.get("hpa")
    storage_data = data.get("storage")
    return TierInfo(
        name=data["name"],
        kind=data["kind"],
        containers=[ContainerInfo(**c) for c in data.get("containers", [])],
        pods=PodCounts(**data.get("pods", {})),
        resources=ResourceInfo(**data.get("resources", {})),
        service=ServiceInfo(**service_data) if service_data else None,
        hpa=HPAInfo(**hpa_data) if hpa_data else None,
        storage=StorageInfo(**storage_data) if storage_data else None,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/{deployment_id}", response_model=VisualizeResponse)
def visualize(
    deployment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> VisualizeResponse:
    """Return live cluster state for a user-owned deployment.

    Falls back to DB-only fields (cluster=null, tiers=[]) with an error
    message when Kubernetes is unreachable (spec §6.5).
    """
    deployment = db.get(Deployment, deployment_id)
    if deployment is None or deployment.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment not found")

    plans = (
        db.query(RemediationPlan)
        .filter(RemediationPlan.deployment_id == deployment_id)
        .order_by(RemediationPlan.created_at)
        .all()
    )
    plan_refs = [
        RemediationPlanRef(
            id=p.id,
            analysis=p.analysis,
            approved=p.approved,
            applied=p.applied,
            source=p.source,
            created_at=p.created_at,
        )
        for p in plans
    ]

    base: dict[str, Any] = dict(
        deployment_id=deployment.id,
        app_name=deployment.app_name,
        status=deployment.status,
        namespace=user.namespace,
        created_at=deployment.created_at,
        updated_at=deployment.updated_at,
        traffic=None,
        remediation_plans=plan_refs,
    )

    try:
        k8s = KubernetesService.get_instance()
        ns_status = k8s.get_namespace_status(user.namespace)
        return VisualizeResponse(
            cluster=_build_cluster(ns_status["cluster"]),
            tiers=[_build_tier(cast(dict[str, Any], t)) for t in ns_status.get("tiers", [])],
            **base,
        )
    except Exception as exc:
        logger.warning("K8s unavailable for visualize(%s): %s", deployment_id, exc)
        return VisualizeResponse(
            cluster=None,
            tiers=[],
            error=f"Kubernetes unavailable: {exc}",
            **base,
        )
