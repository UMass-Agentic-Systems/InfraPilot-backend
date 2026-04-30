"""Tests for the SRE monitor REST endpoints (UC5–UC7, issue #13)."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import Deployment, RemediationPlan, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    *,
    source: str = "manual",
    approved: bool = False,
    applied: bool = False,
    created_at: datetime | None = None,
    analysis: str = "root cause",
    rationale: str = "why",
) -> RemediationPlan:
    plan = RemediationPlan(
        deployment_id=deployment_id,
        event_summary="[]",
        analysis=analysis,
        plan_json='{"actions": []}',
        rationale=rationale,
        approved=approved,
        applied=applied,
        source=source,
    )
    if created_at is not None:
        plan.created_at = created_at
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


# ---------------------------------------------------------------------------
# POST /api/v1/monitor/scan  — UC5
# ---------------------------------------------------------------------------


def test_scan_awaiting_approval(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)

    async def fake_run(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["user_id"] == user.id
        assert kwargs["namespace"] == user.namespace
        assert kwargs["deployment_id"] == deployment.id
        assert kwargs["source"] == "manual"
        return {
            "plan_db_id": 42,
            "analysis": "pods crashlooping",
            "rationale": "image tag invalid",
            "status": "awaiting_approval",
        }

    monkeypatch.setattr("app.api.v1.monitor.run_sre_agent", fake_run)

    resp = client.post(
        "/api/v1/monitor/scan",
        json={"deployment_id": deployment.id},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "plan_db_id": 42,
        "analysis": "pods crashlooping",
        "rationale": "image tag invalid",
        "status": "awaiting_approval",
    }


def test_scan_no_issues(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)

    async def fake_run(**_: Any) -> dict[str, Any]:
        return {
            "plan_db_id": None,
            "analysis": None,
            "rationale": None,
            "status": "no_issues",
        }

    monkeypatch.setattr("app.api.v1.monitor.run_sre_agent", fake_run)

    resp = client.post(
        "/api/v1/monitor/scan",
        json={"deployment_id": deployment.id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "plan_db_id": None,
        "analysis": None,
        "rationale": None,
        "status": "no_issues",
    }


def test_scan_unknown_deployment_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/api/v1/monitor/scan",
        json={"deployment_id": 99999},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_scan_other_user_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Cross-user scan collapses to 404 (no info leakage about ID existence)."""
    other = _seed_other_user(db_session)
    deployment = _seed_deployment(db_session, other.id)

    resp = client.post(
        "/api/v1/monitor/scan",
        json={"deployment_id": deployment.id},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert resp.status_code != 403


def test_scan_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/v1/monitor/scan", json={"deployment_id": 1})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/monitor/plans  — UC6
# ---------------------------------------------------------------------------


def test_list_plans_returns_only_owned_ordered_desc(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    other = _seed_other_user(db_session)

    mine = _seed_deployment(db_session, user.id, app_name="mine")
    theirs = _seed_deployment(db_session, other.id, app_name="theirs")

    now = datetime.now(timezone.utc)
    older = _seed_plan(db_session, mine.id, source="manual", created_at=now - timedelta(hours=2))
    newer = _seed_plan(db_session, mine.id, source="background", created_at=now)
    _seed_plan(db_session, theirs.id, source="manual", created_at=now - timedelta(hours=1))

    resp = client.get("/api/v1/monitor/plans", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [p["id"] for p in body] == [newer.id, older.id]
    assert {p["source"] for p in body} == {"manual", "background"}
    assert all(p["deployment_id"] == mine.id for p in body)


def test_list_plans_empty_is_not_an_error(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.get("/api/v1/monitor/plans", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_plans_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/monitor/plans")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/monitor/plans/{id}/approve  — UC7
# ---------------------------------------------------------------------------


def test_approve_plan_happy_path(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)
    plan = _seed_plan(db_session, deployment.id)

    captured: dict[str, Any] = {}

    async def fake_resume(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"plan_id": plan.id, "approved": True, "applied": True}

    monkeypatch.setattr("app.api.v1.monitor.resume_sre_agent", fake_resume)

    resp = client.post(
        f"/api/v1/monitor/plans/{plan.id}/approve",
        json={"approved": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"plan_id": plan.id, "approved": True, "applied": True}
    assert captured == {
        "user_id": user.id,
        "deployment_id": deployment.id,
        "approved": True,
        "db": captured["db"],  # session instance — presence checked above
    }


def test_approve_plan_rejection_does_not_apply(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    deployment = _seed_deployment(db_session, user.id)
    plan = _seed_plan(db_session, deployment.id)

    async def fake_resume(**_: Any) -> dict[str, Any]:
        return {"plan_id": plan.id, "approved": False, "applied": False}

    monkeypatch.setattr("app.api.v1.monitor.resume_sre_agent", fake_resume)

    resp = client.post(
        f"/api/v1/monitor/plans/{plan.id}/approve",
        json={"approved": False},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["approved"] is False
    assert body["applied"] is False


def test_approve_plan_unknown_id_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/api/v1/monitor/plans/99999/approve",
        json={"approved": True},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_approve_plan_other_user_returns_403(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    """Plan owned by another user → 403 (issue #13: distinct from the 404 elsewhere)."""
    other = _seed_other_user(db_session)
    deployment = _seed_deployment(db_session, other.id)
    plan = _seed_plan(db_session, deployment.id)

    resp = client.post(
        f"/api/v1/monitor/plans/{plan.id}/approve",
        json={"approved": True},
        headers=auth_headers,
    )
    assert resp.status_code == 403


def test_approve_plan_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/v1/monitor/plans/1/approve", json={"approved": True})
    assert resp.status_code == 401
