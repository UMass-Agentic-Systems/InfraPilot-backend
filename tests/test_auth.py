from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_current_user, get_db
from app.api.v1.auth import _derive_namespace
from app.core.security import create_access_token
from app.db.models import User
from app.services import k8s_client as k8s_client_module


# ---------------------------------------------------------------------------
# _derive_namespace — pure unit tests
# ---------------------------------------------------------------------------


def test_derive_namespace_jerin() -> None:
    assert _derive_namespace("jerin.thomas@umass.edu") == "user-jerin-thomas"


def test_derive_namespace_uppercase_lowercased() -> None:
    assert _derive_namespace("JerinThomas@umass.edu") == "user-jerinthomas"


def test_derive_namespace_special_chars() -> None:
    assert _derive_namespace("a+b!c@x.com") == "user-a-b-c"


def test_derive_namespace_truncates_long_local_part() -> None:
    long_local = "a" * 100
    ns = _derive_namespace(f"{long_local}@x.com")
    # "user-" + 58 a's = 63 chars total (matches User.namespace String(63))
    assert ns == "user-" + "a" * 58
    assert len(ns) == 63


# ---------------------------------------------------------------------------
# /register
# ---------------------------------------------------------------------------


def test_register_returns_201_and_token(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "jerin.thomas@umass.edu", "password": "hunter2"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_register_duplicate_email_409(client: TestClient) -> None:
    payload = {"email": "dup@umass.edu", "password": "hunter2"}
    assert client.post("/api/v1/auth/register", json=payload).status_code == 201
    second = client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409


def test_register_namespace_failure_non_fatal(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = MagicMock()
    failing.ensure_namespace.side_effect = RuntimeError("cluster down")
    monkeypatch.setattr(
        k8s_client_module.KubernetesService,
        "get_instance",
        classmethod(lambda cls: failing),
    )

    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@x.com", "password": "hunter2"},
    )
    assert resp.status_code == 201
    assert failing.ensure_namespace.call_count == 1


def test_register_does_not_store_plaintext(client: TestClient) -> None:
    raw = "supersecret-plaintext-1234"
    client.post(
        "/api/v1/auth/register",
        json={"email": "bob@x.com", "password": raw},
    )
    # Pull the User row directly via the same overridden Session factory.
    db_gen = client.app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        user = db.query(User).filter(User.email == "bob@x.com").one()
        assert user.hashed_password != raw
        assert raw not in user.hashed_password
        assert user.namespace == "user-bob"
    finally:
        db_gen.close()


def test_register_invalid_email_422(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": "hunter2"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------


def _register(client: TestClient, email: str, password: str) -> str:
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 201
    return str(resp.json()["access_token"])


def test_login_success(client: TestClient) -> None:
    _register(client, "carol@x.com", "hunter2")
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "carol@x.com", "password": "hunter2"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_login_wrong_password_401(client: TestClient) -> None:
    _register(client, "dan@x.com", "hunter2")
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "dan@x.com", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_login_unknown_email_401(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "ghost@x.com", "password": "hunter2"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# get_current_user — exercised via a probe route mounted on the same app
# ---------------------------------------------------------------------------


_probe_app: FastAPI | None = None


def _attach_probe(client: TestClient) -> None:
    """Mount /probe on the live app once. Reuses the test's get_db override."""
    global _probe_app
    app = client.app
    if any(getattr(r, "path", None) == "/probe" for r in app.routes):
        return

    def probe(user: User = Depends(get_current_user)) -> dict[str, str]:
        return {"email": user.email}

    app.add_api_route("/probe", probe, methods=["GET"])


def test_get_current_user_valid_token(client: TestClient) -> None:
    _attach_probe(client)
    token = _register(client, "eve@x.com", "hunter2")
    resp = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json() == {"email": "eve@x.com"}


def test_get_current_user_missing_token_401(client: TestClient) -> None:
    _attach_probe(client)
    resp = client.get("/probe")
    assert resp.status_code == 401


def test_get_current_user_malformed_token_401(client: TestClient) -> None:
    _attach_probe(client)
    resp = client.get("/probe", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_get_current_user_expired_token_401(client: TestClient) -> None:
    _attach_probe(client)
    token = create_access_token("1", expires_delta=timedelta(seconds=-1))
    resp = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_get_current_user_unknown_user_id_401(client: TestClient) -> None:
    _attach_probe(client)
    # Valid signature, but no user with id=999 exists in the test DB.
    token = create_access_token("999")
    resp = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
