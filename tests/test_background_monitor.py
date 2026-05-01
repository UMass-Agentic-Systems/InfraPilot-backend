"""Background SRE monitor integration tests (issue #15, UC18/UC19/UC20).

Exercises `_scan_all_users` and `sre_background_loop` end-to-end with mocked
Kubernetes, ConnectionManager, and `run_sre_agent`. The K8s/Gemini boundaries
are never crossed in these tests.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    ChatMessage,
    ChatSession,
    Deployment,
    RemediationPlan,
    User,
)
from app.services import sre_background
from app.services.sre_background import _scan_all_users, sre_background_loop


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _seed_user(db: Session, email: str = "u@example.com", namespace: str = "user-u") -> User:
    user = User(email=email, hashed_password="hash", namespace=namespace)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_deployment(
    db: Session,
    user_id: int,
    *,
    app_name: str = "web",
    status: str = "deployed",
    chat_session_id: int | None = None,
) -> Deployment:
    deployment = Deployment(
        user_id=user_id,
        app_name=app_name,
        desired_state_yaml="",
        status=status,
        chat_session_id=chat_session_id,
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


def _seed_chat_session(db: Session, user_id: int) -> ChatSession:
    session = ChatSession(user_id=user_id, title="t")
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _seed_plan(
    db: Session,
    deployment_id: int,
    *,
    source: str = "background",
    approved: bool = False,
    applied: bool = False,
    event_summary: str = "[]",
    created_at: datetime | None = None,
) -> RemediationPlan:
    plan = RemediationPlan(
        deployment_id=deployment_id,
        event_summary=event_summary,
        analysis="a",
        plan_json='{"actions": []}',
        rationale="r",
        approved=approved,
        applied=applied,
        source=source,
    )
    if created_at is not None:
        plan.created_at = created_at
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


_POD_EVENT: dict[str, Any] = {
    "reason": "BackOff",
    "message": "Back-off restarting failed container",
    "involved_object": {"kind": "Pod", "name": "web-abc-123", "namespace": "user-u"},
    "count": 5,
    "last_timestamp": "2026-04-30T00:00:00",
}


def _mock_k8s(events: list[dict[str, Any]]) -> MagicMock:
    k8s = MagicMock()
    k8s.get_warning_events = MagicMock(return_value=list(events))
    return k8s


def _mock_manager(*, connected: bool = True, session_id: int | None = 1) -> MagicMock:
    mgr = MagicMock()
    mgr.is_user_connected = AsyncMock(return_value=connected)
    mgr.get_active_session = AsyncMock(return_value=session_id)
    mgr.send_to_user = AsyncMock()
    return mgr


def _fake_run_sre_agent(plan_db_id: int | None, status: str = "awaiting_approval") -> Any:
    async def _run(**_kwargs: Any) -> dict[str, Any]:
        return {
            "plan_db_id": plan_db_id,
            "analysis": "pods crashlooping",
            "rationale": "image tag invalid",
            "status": status,
        }

    return _run


# ---------------------------------------------------------------------------
# sre_background_loop
# ---------------------------------------------------------------------------


async def test_loop_reraises_cancelled_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_kwargs: Any) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(sre_background, "_scan_all_users", boom)

    with pytest.raises(asyncio.CancelledError):
        await sre_background_loop()


async def test_loop_continues_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-cancel errors are logged; the loop sleeps and is then cancellable."""
    calls = {"scan": 0, "sleep_args": []}  # type: dict[str, Any]

    async def flaky(**_kwargs: Any) -> None:
        calls["scan"] += 1
        if calls["scan"] == 1:
            raise RuntimeError("boom")
        raise asyncio.CancelledError()

    async def fake_sleep(seconds: float) -> None:
        calls["sleep_args"].append(seconds)

    monkeypatch.setattr(sre_background, "_scan_all_users", flaky)
    monkeypatch.setattr("app.services.sre_background.asyncio.sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await sre_background_loop()

    assert calls["scan"] == 2
    assert calls["sleep_args"] == [settings.SRE_SCAN_INTERVAL_SECONDS]


# ---------------------------------------------------------------------------
# _scan_all_users — gating, dedup, alert routing
# ---------------------------------------------------------------------------


async def test_scan_skips_users_with_no_deployed_deployments(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _seed_user(db_session)
    _seed_deployment(db_session, user.id, status="pending")

    fake_run = AsyncMock()
    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=_mock_manager(),
        k8s_provider=lambda: _mock_k8s([_POD_EVENT]),
    )

    fake_run.assert_not_awaited()


async def test_scan_skips_when_no_warning_events(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _seed_user(db_session)
    _seed_deployment(db_session, user.id)

    fake_run = AsyncMock()
    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=_mock_manager(),
        k8s_provider=lambda: _mock_k8s([]),
    )

    fake_run.assert_not_awaited()


async def test_scan_runs_agent_for_affected_deployment_with_background_source(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _seed_user(db_session)
    chat = _seed_chat_session(db_session, user.id)
    deployment = _seed_deployment(db_session, user.id, app_name="web")
    _seed_deployment(db_session, user.id, app_name="other")  # not affected

    captured: dict[str, Any] = {}

    async def fake_run(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        plan = _seed_plan(db_session, deployment.id, source="background")
        return {
            "plan_db_id": plan.id,
            "analysis": "x",
            "rationale": "y",
            "status": "awaiting_approval",
        }

    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    mgr = _mock_manager(connected=True, session_id=chat.id)
    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=mgr,
        k8s_provider=lambda: _mock_k8s([_POD_EVENT]),
    )

    assert captured["user_id"] == user.id
    assert captured["namespace"] == user.namespace
    assert captured["deployment_id"] == deployment.id
    assert captured["source"] == "background"


async def test_scan_time_based_dedup_skips_recent_unresolved_plan(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _seed_user(db_session)
    deployment = _seed_deployment(db_session, user.id, app_name="web")
    _seed_plan(db_session, deployment.id, created_at=datetime.now(timezone.utc))

    fake_run = AsyncMock()
    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=_mock_manager(),
        k8s_provider=lambda: _mock_k8s([_POD_EVENT]),
    )

    fake_run.assert_not_awaited()


async def test_scan_event_based_dedup_skips_when_events_match_prior(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _seed_user(db_session)
    deployment = _seed_deployment(db_session, user.id, app_name="web")
    # Older than the time window so only event-based dedup can save us.
    old = datetime.now(timezone.utc) - timedelta(seconds=settings.SRE_SCAN_INTERVAL_SECONDS * 5)
    _seed_plan(
        db_session,
        deployment.id,
        event_summary=json.dumps([_POD_EVENT]),
        created_at=old,
    )

    fake_run = AsyncMock()
    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=_mock_manager(),
        k8s_provider=lambda: _mock_k8s([_POD_EVENT]),
    )

    fake_run.assert_not_awaited()


async def test_scan_persists_chat_message_before_ws_send(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NFR §12.7 / issue #15 acceptance: DB commit must precede WebSocket send."""
    user = _seed_user(db_session)
    chat = _seed_chat_session(db_session, user.id)
    deployment = _seed_deployment(db_session, user.id, app_name="web")

    async def fake_run(**_kwargs: Any) -> dict[str, Any]:
        plan = _seed_plan(db_session, deployment.id, source="background")
        return {
            "plan_db_id": plan.id,
            "analysis": "x",
            "rationale": "y",
            "status": "awaiting_approval",
        }

    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    observed: dict[str, Any] = {}

    async def capture_send(user_id: int, payload: dict[str, Any]) -> None:
        observed["row_count_at_send"] = (
            db_session.query(ChatMessage).filter(ChatMessage.session_id == chat.id).count()
        )
        observed["payload"] = payload

    mgr = _mock_manager(connected=True, session_id=chat.id)
    mgr.send_to_user = AsyncMock(side_effect=capture_send)

    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=mgr,
        k8s_provider=lambda: _mock_k8s([_POD_EVENT]),
    )

    assert observed["row_count_at_send"] == 1
    payload = observed["payload"]
    assert payload["type"] == "sre_alert"
    assert payload["deployment_id"] == deployment.id
    assert payload["app_name"] == "web"
    assert payload["message"]["role"] == "sre-agent"
    metadata = json.loads(payload["message"]["metadata_json"])
    assert metadata["source"] == "background"
    assert metadata["status"] == "awaiting_approval"
    assert metadata["deployment_id"] == deployment.id


async def test_scan_no_chat_message_when_user_disconnected(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UC19: disconnected user → plan persists, no chat row, no WS send."""
    user = _seed_user(db_session)
    _seed_chat_session(db_session, user.id)
    deployment = _seed_deployment(db_session, user.id, app_name="web")

    async def fake_run(**_kwargs: Any) -> dict[str, Any]:
        plan = _seed_plan(db_session, deployment.id, source="background")
        return {
            "plan_db_id": plan.id,
            "analysis": "x",
            "rationale": "y",
            "status": "awaiting_approval",
        }

    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    mgr = _mock_manager(connected=False, session_id=None)
    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=mgr,
        k8s_provider=lambda: _mock_k8s([_POD_EVENT]),
    )

    assert db_session.query(ChatMessage).count() == 0
    assert db_session.query(RemediationPlan).count() == 1
    mgr.send_to_user.assert_not_awaited()
    mgr.get_active_session.assert_not_awaited()


async def test_scan_does_not_alert_when_agent_returns_no_issues(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _seed_user(db_session)
    _seed_chat_session(db_session, user.id)
    _seed_deployment(db_session, user.id, app_name="web")

    monkeypatch.setattr(
        sre_background,
        "run_sre_agent",
        _fake_run_sre_agent(plan_db_id=None, status="no_issues"),
    )

    mgr = _mock_manager(connected=True)
    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=mgr,
        k8s_provider=lambda: _mock_k8s([_POD_EVENT]),
    )

    assert db_session.query(ChatMessage).count() == 0
    mgr.send_to_user.assert_not_awaited()


async def test_per_user_exception_does_not_affect_other_users(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = _seed_user(db_session, email="bad@example.com", namespace="user-bad")
    good = _seed_user(db_session, email="good@example.com", namespace="user-good")
    _seed_deployment(db_session, bad.id, app_name="web")
    good_dep = _seed_deployment(db_session, good.id, app_name="web")
    good_chat = _seed_chat_session(db_session, good.id)

    def get_warning_events(namespace: str) -> list[dict[str, Any]]:
        if namespace == "user-bad":
            raise RuntimeError("k8s blew up for this user")
        return [
            dict(
                _POD_EVENT, involved_object={"kind": "Pod", "name": "web-1", "namespace": namespace}
            )
        ]

    k8s = MagicMock()
    k8s.get_warning_events = MagicMock(side_effect=get_warning_events)

    async def fake_run(**kwargs: Any) -> dict[str, Any]:
        plan = _seed_plan(db_session, good_dep.id, source="background")
        return {
            "plan_db_id": plan.id,
            "analysis": "x",
            "rationale": "y",
            "status": "awaiting_approval",
        }

    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    mgr = _mock_manager(connected=True, session_id=good_chat.id)
    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=mgr,
        k8s_provider=lambda: k8s,
    )

    mgr.send_to_user.assert_awaited_once()
    sent = mgr.send_to_user.await_args.args[1]
    assert sent["app_name"] == "web"
    assert db_session.query(RemediationPlan).count() == 1


async def test_k8s_unreachable_skips_iteration(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _seed_user(db_session)
    _seed_deployment(db_session, user.id, app_name="web")

    fake_run = AsyncMock()
    monkeypatch.setattr(sre_background, "run_sre_agent", fake_run)

    def boom() -> Any:
        raise RuntimeError("kube API unreachable")

    await _scan_all_users(
        db_factory=lambda: db_session,
        ws_manager=_mock_manager(),
        k8s_provider=boom,
    )

    fake_run.assert_not_awaited()
