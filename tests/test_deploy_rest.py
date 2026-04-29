"""Tests for the standalone deploy REST endpoints (UC3 + UC4 + A1 fix)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import Deployment, User


# ---------------------------------------------------------------------------
# POST /api/v1/deploy/  — UC3 + A1 fix
# ---------------------------------------------------------------------------


def test_deploy_post_failure_returns_200_with_status_failed(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """A1 fix: agent raise -> HTTP 200 with status='failed', not an HTTP error.

    The provisioning agent stub always raises NotImplementedError, so this
    test exercises the failure path against the real handler.
    """
    resp = client.post(
        "/api/v1/deploy/",
        json={"app_name": "myapp", "requirements": "react frontend"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deployment_id"] is None
    assert body["status"] == "failed"
    assert body["error"]


def test_deploy_post_requires_auth(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/deploy/",
        json={"app_name": "x", "requirements": "y"},
    )
    assert resp.status_code == 401


def test_deploy_post_validates_required_fields(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.post("/api/v1/deploy/", json={}, headers=auth_headers)
    assert resp.status_code == 422


def test_deploy_post_passes_chat_session_id_none(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #3: standalone POST always invokes the agent with chat_session_id=None."""
    captured: dict[str, Any] = {}

    async def fake_agent(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"deployment_id": 99, "status": "deployed", "error": None}

    monkeypatch.setattr("app.api.v1.deploy.run_provisioning_agent", fake_agent)

    resp = client.post(
        "/api/v1/deploy/",
        json={"app_name": "myapp", "requirements": "redis cache"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deployed"
    assert captured["chat_session_id"] is None
    assert captured["app_name"] == "myapp"
    assert captured["requirements"] == "redis cache"
    # Sanity: the user's namespace is forwarded.
    assert captured["namespace"] == "user-test"


# ---------------------------------------------------------------------------
# GET /api/v1/deploy/{id}  — UC4
# ---------------------------------------------------------------------------


def _seed_deployment(db_session: Session, user_id: int, **fields: Any) -> Deployment:
    deployment = Deployment(
        user_id=user_id,
        app_name=fields.get("app_name", "seeded-app"),
        desired_state_yaml=fields.get("desired_state_yaml", "apiVersion: v1\nkind: Pod\n"),
        status=fields.get("status", "deployed"),
    )
    db_session.add(deployment)
    db_session.commit()
    db_session.refresh(deployment)
    return deployment


def test_get_deployment_owner_can_read(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id, app_name="myapp")

    resp = client.get(f"/api/v1/deploy/{deployment.id}", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == deployment.id
    assert body["app_name"] == "myapp"
    assert body["status"] == "deployed"
    assert body["desired_state_yaml"] == "apiVersion: v1\nkind: Pod\n"


def test_get_deployment_other_user_returns_404_not_403(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """UC4: cross-user read collapses to 404 (no info leakage about which IDs exist)."""
    other = User(
        email="other@example.com",
        hashed_password=hash_password("pw"),
        namespace="user-other",
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    deployment = _seed_deployment(db_session, other.id, app_name="theirs")

    resp = client.get(f"/api/v1/deploy/{deployment.id}", headers=auth_headers)
    assert resp.status_code == 404
    # 403 would leak that the deployment exists; we explicitly avoid it.
    assert resp.status_code != 403


def test_get_deployment_unknown_id_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.get("/api/v1/deploy/99999", headers=auth_headers)
    assert resp.status_code == 404


def test_get_deployment_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/deploy/1")
    assert resp.status_code == 401
