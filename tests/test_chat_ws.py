"""Tests for the chat WebSocket endpoint (issue #17)."""

from typing import Any

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import ChatMessage, ChatSession, Deployment, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(db_session: Session, user_id: int, title: str = "Test") -> ChatSession:
    session = ChatSession(user_id=user_id, title=title)
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def _seed_deployment(db_session: Session, user_id: int) -> Deployment:
    deployment = Deployment(
        user_id=user_id,
        app_name="web",
        desired_state_yaml="",
        status="deployed",
    )
    db_session.add(deployment)
    db_session.commit()
    db_session.refresh(deployment)
    return deployment


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


@pytest.fixture
def auth_token(auth_headers: dict[str, str]) -> str:
    return auth_headers["Authorization"].removeprefix("Bearer ")


def _ws_url(session_id: int, token: str) -> str:
    return f"/api/v1/chat/sessions/{session_id}/ws?token={token}"


# ---------------------------------------------------------------------------
# Authentication / authorisation rejection
# ---------------------------------------------------------------------------


def test_ws_invalid_token_closes_4001(client: TestClient) -> None:
    try:
        with client.websocket_connect("/api/v1/chat/sessions/1/ws?token=bad-token") as ws:
            ws.receive_json()
        pytest.fail("Expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 4001


def test_ws_unknown_session_closes_4004(
    client: TestClient,
    auth_token: str,
) -> None:
    try:
        with client.websocket_connect(_ws_url(99999, auth_token)) as ws:
            ws.receive_json()
        pytest.fail("Expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 4004


def test_ws_other_user_session_closes_4004(
    client: TestClient,
    auth_token: str,
    db_session: Session,
) -> None:
    other = _seed_other_user(db_session)
    session = _seed_session(db_session, other.id)

    try:
        with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
            ws.receive_json()
        pytest.fail("Expected WebSocketDisconnect")
    except WebSocketDisconnect as exc:
        assert exc.code == 4004


# ---------------------------------------------------------------------------
# Intent detection — general (no agent call)
# ---------------------------------------------------------------------------


def test_ws_general_intent_response(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "hello there"})

        typing_start = ws.receive_json()
        assert typing_start["type"] == "typing"
        assert typing_start["agent"] == "infra-agent"
        assert typing_start["is_typing"] is True

        typing_stop = ws.receive_json()
        assert typing_stop["type"] == "typing"
        assert typing_stop["is_typing"] is False

        response = ws.receive_json()
        assert response["type"] == "agent_response"
        assert "deploy" in response["message"]["content"].lower()


# ---------------------------------------------------------------------------
# Intent detection — deploy
# ---------------------------------------------------------------------------


def test_ws_deploy_intent_routes_infra_agent(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    async def fake_provision(**kwargs: Any) -> dict[str, Any]:
        return {"deployment_id": 42, "status": "deployed", "error": None}

    monkeypatch.setattr("app.api.v1.chat.run_provisioning_agent", fake_provision)

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "deploy my nginx app"})

        typing_start = ws.receive_json()
        assert typing_start["agent"] == "infra-agent"
        assert typing_start["is_typing"] is True

        ws.receive_json()  # typing stop

        response = ws.receive_json()
        assert response["type"] == "agent_response"
        assert "deployed" in response["message"]["content"].lower()


def test_ws_deploy_failure_surfaced_in_response(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    async def fake_provision(**kwargs: Any) -> dict[str, Any]:
        return {"deployment_id": None, "status": "failed", "error": "YAML invalid"}

    monkeypatch.setattr("app.api.v1.chat.run_provisioning_agent", fake_provision)

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "deploy my app"})
        ws.receive_json()  # typing start
        ws.receive_json()  # typing stop
        response = ws.receive_json()

    assert "failed" in response["message"]["content"].lower()
    assert "YAML invalid" in response["message"]["content"]


# ---------------------------------------------------------------------------
# Intent detection — sre
# ---------------------------------------------------------------------------


def test_ws_sre_intent_routes_sre_agent(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)
    _seed_deployment(db_session, user.id)

    async def fake_sre(**kwargs: Any) -> dict[str, Any]:
        return {
            "plan_db_id": 99,
            "analysis": "pods crashlooping",
            "rationale": "bad image",
            "status": "awaiting_approval",
        }

    monkeypatch.setattr("app.api.v1.chat.run_sre_agent", fake_sre)

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "scan my deployment"})

        typing_start = ws.receive_json()
        assert typing_start["agent"] == "sre-agent"
        assert typing_start["is_typing"] is True

        ws.receive_json()  # typing stop

        response = ws.receive_json()
        assert response["type"] == "agent_response"
        assert "pods crashlooping" in response["message"]["content"]


def test_ws_sre_no_deployments_returns_helpful_message(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)
    # No deployments seeded.

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "scan my deployment"})
        ws.receive_json()  # typing start
        ws.receive_json()  # typing stop
        response = ws.receive_json()

    assert "no deployment" in response["message"]["content"].lower()


def test_ws_sre_no_issues_response(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)
    _seed_deployment(db_session, user.id)

    async def fake_sre(**kwargs: Any) -> dict[str, Any]:
        return {"plan_db_id": None, "analysis": None, "rationale": None, "status": "no_issues"}

    monkeypatch.setattr("app.api.v1.chat.run_sre_agent", fake_sre)

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "monitor my app"})
        ws.receive_json()
        ws.receive_json()
        response = ws.receive_json()

    assert "no issues" in response["message"]["content"].lower()


# ---------------------------------------------------------------------------
# Message persistence
# ---------------------------------------------------------------------------


def test_ws_user_and_agent_messages_persisted(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "hello world"})
        ws.receive_json()  # typing start
        ws.receive_json()  # typing stop
        ws.receive_json()  # agent response

    db_session.expire_all()
    messages = (
        db_session.query(ChatMessage)
        .filter(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.position)
        .all()
    )
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == "hello world"
    assert messages[0].position == 0
    assert messages[1].role == "infra-agent"
    assert messages[1].position == 1


def test_ws_session_updated_at_refreshed(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)
    original_updated_at = session.updated_at

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "message", "content": "hello"})
        ws.receive_json()
        ws.receive_json()
        ws.receive_json()

    db_session.expire_all()
    refreshed = db_session.get(ChatSession, session.id)
    assert refreshed is not None
    assert refreshed.updated_at >= original_updated_at


# ---------------------------------------------------------------------------
# Invalid message format
# ---------------------------------------------------------------------------


def test_ws_invalid_message_format_returns_error(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    with client.websocket_connect(_ws_url(session.id, auth_token)) as ws:
        ws.send_json({"type": "unknown", "data": "bad"})
        error = ws.receive_json()

    assert error["type"] == "error"
    assert "invalid" in error["detail"].lower()
