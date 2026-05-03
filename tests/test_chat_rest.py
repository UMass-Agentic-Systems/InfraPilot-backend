"""Tests for the chat session REST endpoints (issue #16)."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.db.models import ChatMessage, ChatSession, User


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_session(db_session: Session, user_id: int, **fields: Any) -> ChatSession:
    session = ChatSession(
        user_id=user_id,
        title=fields.get("title", "Test Session"),
    )
    if "updated_at" in fields:
        session.updated_at = fields["updated_at"]
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def _seed_message(
    db_session: Session,
    session_id: int,
    position: int,
    role: str = "user",
    content: str = "hello",
) -> ChatMessage:
    msg = ChatMessage(
        session_id=session_id,
        role=role,
        content=content,
        position=position,
    )
    db_session.add(msg)
    db_session.commit()
    db_session.refresh(msg)
    return msg


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
# POST /api/v1/chat/sessions
# ---------------------------------------------------------------------------


def test_create_session_default_title(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.post("/api/v1/chat/sessions", json={}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "New Chat"
    assert "id" in body
    assert "created_at" in body
    assert "updated_at" in body


def test_create_session_custom_title(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.post("/api/v1/chat/sessions", json={"title": "My Session"}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["title"] == "My Session"


def test_create_session_requires_auth(client: TestClient) -> None:
    resp = client.post("/api/v1/chat/sessions", json={})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/chat/sessions
# ---------------------------------------------------------------------------


def test_list_sessions_empty(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/chat/sessions", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_sessions_sorted_by_recency(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    now = datetime.now(timezone.utc)
    older = _seed_session(db_session, user.id, title="Older", updated_at=now - timedelta(hours=2))
    newer = _seed_session(db_session, user.id, title="Newer", updated_at=now)

    resp = client.get("/api/v1/chat/sessions", headers=auth_headers)
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()]
    assert ids == [newer.id, older.id]


def test_list_sessions_excludes_other_users(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    other = _seed_other_user(db_session)

    _seed_session(db_session, user.id, title="Mine")
    _seed_session(db_session, other.id, title="Theirs")

    resp = client.get("/api/v1/chat/sessions", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["title"] == "Mine"


def test_list_sessions_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/chat/sessions")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/chat/sessions/{session_id}
# ---------------------------------------------------------------------------


def test_get_session_with_messages(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id, title="Detailed")
    _seed_message(db_session, session.id, position=0, content="hello")
    _seed_message(db_session, session.id, position=1, role="infra-agent", content="world")

    resp = client.get(f"/api/v1/chat/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == session.id
    assert body["title"] == "Detailed"
    assert len(body["messages"]) == 2
    assert body["messages"][0]["position"] == 0
    assert body["messages"][1]["position"] == 1


def test_get_session_no_messages(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    resp = client.get(f"/api/v1/chat/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["messages"] == []


def test_get_session_limits_to_200_messages(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)
    for i in range(205):
        _seed_message(db_session, session.id, position=i)

    resp = client.get(f"/api/v1/chat/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 200


def test_get_session_unknown_returns_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    resp = client.get("/api/v1/chat/sessions/99999", headers=auth_headers)
    assert resp.status_code == 404


def test_get_session_other_user_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    other = _seed_other_user(db_session)
    session = _seed_session(db_session, other.id)

    resp = client.get(f"/api/v1/chat/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 404


def test_get_session_requires_auth(client: TestClient) -> None:
    resp = client.get("/api/v1/chat/sessions/1")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /api/v1/chat/sessions/{session_id}
# ---------------------------------------------------------------------------


def test_delete_session_returns_204(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    resp = client.delete(f"/api/v1/chat/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 204


def test_delete_session_removes_from_db(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)
    session_id = session.id

    client.delete(f"/api/v1/chat/sessions/{session_id}", headers=auth_headers)

    db_session.expire_all()
    assert db_session.get(ChatSession, session_id) is None


def test_delete_session_unknown_returns_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    resp = client.delete("/api/v1/chat/sessions/99999", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_session_other_user_returns_404(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
) -> None:
    other = _seed_other_user(db_session)
    session = _seed_session(db_session, other.id)

    resp = client.delete(f"/api/v1/chat/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 404


def test_delete_session_requires_auth(client: TestClient) -> None:
    resp = client.delete("/api/v1/chat/sessions/1")
    assert resp.status_code == 401


def test_delete_session_closes_active_websocket(
    client: TestClient,
    auth_headers: dict[str, str],
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = db_session.query(User).filter(User.email == "test@example.com").one()
    session = _seed_session(db_session, user.id)

    closed_with: dict[str, Any] = {}

    async def fake_close_session(user_id: int, session_id: int, code: int) -> None:
        closed_with.update({"user_id": user_id, "session_id": session_id, "code": code})

    monkeypatch.setattr("app.api.v1.chat.manager.close_session", fake_close_session)

    resp = client.delete(f"/api/v1/chat/sessions/{session.id}", headers=auth_headers)
    assert resp.status_code == 204
    assert closed_with == {"user_id": user.id, "session_id": session.id, "code": 4010}
