"""Tests for the visualization endpoint (issue #18)."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.services.k8s_client as k8s_client_module
from app.core.security import hash_password
from app.db.models import Deployment, RemediationPlan, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NAMESPACE_STATUS: dict[str, Any] = {
    "cluster": {
        "name": "minikube",
        "provider": "unknown",
        "region": "unknown",
        "k8s_version": "v1.28.0",
        "nodes": {"ready": 1, "total": 1},
    },
    "tiers": [
        {
            "name": "web",
            "kind": "Deployment",
            "containers": [{"name": "web", "image": "nginx:latest"}],
            "pods": {"running": 1, "pending": 0, "failed": 0, "total": 1},
            "resources": {
                "cpu_usage_percent": None,
                "memory_usage_percent": None,
                "cpu_requests": "100m",
                "memory_requests": "128Mi",
            },
            "service": {"type": "ClusterIP", "port": 80},
            "hpa": None,
            "storage": None,
        }
    ],
}


def _seed_deployment(db_session: Session, user_id: int, **fields: Any) -> Deployment:
    deployment = Deployment(
        user_id=user_id,
        app_name=fields.get("app_name", "web"),
        desired_state_yaml=fields.get("desired_state_yaml", ""),
        status=fields.get("status", "deployed"),
    )
    db_session.add(deployment)
    db_session.commit()
    db_session.refresh(deployment)
    return deployment


def _seed_plan(
    db_session: Session,
    deployment_id: int,
    source: str = "manual",
    approved: bool = False,
    applied: bool = False,
) -> RemediationPlan:
    plan = RemediationPlan(
        deployment_id=deployment_id,
        event_summary="[]",
        analysis="pods failing",
        plan_json='{"actions": []}',
        rationale="fix it",
        approved=approved,
        applied=applied,
        source=source,
    )
    db_session.add(plan)
    db_session.commit()
    db_session.refresh(plan)
    return plan


def _seed_other_user(db_session: Session) -> User:
    other = User(
        email="other@example.com",
        hashed_password=hash_password("pw"),
        namespace="user-other",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    return other


def _mock_k8s(monkeypatch: pytest.MonkeyPatch, ns_status: dict[str, Any]) -> MagicMock:
    mock = MagicMock()
    mock.get_namespace_status.return_value = ns_status
    monkeypatch.setattr(
        k8s_client_module.KubernetesService,
        "get_instance",
        classmethod(lambda cls: mock),
    )
    return mock


# ---------------------------------------------------------------------------
# Happy path — K8s available
# ---------------------------------------------------------------------------


def test_visualize_returns_cluster_and_tiers(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)
    _mock_k8s(monkeypatch, _NAMESPACE_STATUS)

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deployment_id"] == deployment.id
    assert body["app_name"] == "web"
    assert body["status"] == "deployed"
    assert body["cluster"]["name"] == "minikube"
    assert body["cluster"]["nodes"] == {"ready": 1, "total": 1}
    assert len(body["tiers"]) == 1
    tier = body["tiers"][0]
    assert tier["name"] == "web"
    assert tier["kind"] == "Deployment"
    assert tier["containers"] == [{"name": "web", "image": "nginx:latest"}]
    assert tier["pods"]["running"] == 1
    assert tier["service"] == {"type": "ClusterIP", "port": 80}
    assert tier["hpa"] is None
    assert tier["storage"] is None
    assert body["error"] is None


def test_visualize_traffic_always_null(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)
    _mock_k8s(monkeypatch, _NAMESPACE_STATUS)

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["traffic"] is None


# ---------------------------------------------------------------------------
# K8s unavailable — fallback response
# ---------------------------------------------------------------------------


def test_visualize_k8s_unavailable_returns_fallback(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)

    mock = MagicMock()
    mock.get_namespace_status.side_effect = Exception("connection refused")
    monkeypatch.setattr(
        k8s_client_module.KubernetesService,
        "get_instance",
        classmethod(lambda cls: mock),
    )

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deployment_id"] == deployment.id
    assert body["cluster"] is None
    assert body["tiers"] == []
    assert body["traffic"] is None
    assert body["error"] is not None
    assert "connection refused" in body["error"]


def test_visualize_k8s_instance_error_returns_fallback(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)

    monkeypatch.setattr(
        k8s_client_module.KubernetesService,
        "get_instance",
        classmethod(lambda cls: (_ for _ in ()).throw(Exception("k8s init failed"))),
    )

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["cluster"] is None
    assert body["error"] is not None


# ---------------------------------------------------------------------------
# Remediation plans
# ---------------------------------------------------------------------------


def test_visualize_includes_all_plan_sources(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)
    manual_plan = _seed_plan(db_session, deployment.id, source="manual")
    bg_plan = _seed_plan(db_session, deployment.id, source="background")
    _mock_k8s(monkeypatch, _NAMESPACE_STATUS)

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)

    assert resp.status_code == 200
    plans = resp.json()["remediation_plans"]
    assert len(plans) == 2
    sources = {p["source"] for p in plans}
    assert sources == {"manual", "background"}
    ids = [p["id"] for p in plans]
    assert manual_plan.id in ids and bg_plan.id in ids


def test_visualize_plans_ordered_by_created_at(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)
    first = _seed_plan(db_session, deployment.id, source="manual")
    second = _seed_plan(db_session, deployment.id, source="background")
    _mock_k8s(monkeypatch, _NAMESPACE_STATUS)

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)

    plans = resp.json()["remediation_plans"]
    assert [p["id"] for p in plans] == [first.id, second.id]


def test_visualize_no_plans_returns_empty_list(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)
    _mock_k8s(monkeypatch, _NAMESPACE_STATUS)

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["remediation_plans"] == []


# ---------------------------------------------------------------------------
# Ownership / auth
# ---------------------------------------------------------------------------


def test_visualize_unknown_deployment_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.get("/api/v1/visualize/99999", headers=auth_headers)
    assert resp.status_code == 404


def test_visualize_other_user_deployment_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    other = _seed_other_user(db_session)
    deployment = _seed_deployment(db_session, other.id)

    resp = client.get(f"/api/v1/visualize/{deployment.id}", headers=auth_headers)
    assert resp.status_code == 404


def test_visualize_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/visualize/1")
    assert resp.status_code == 401
