"""Tests for the provisioning LangGraph pipeline (issue #11)."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.agents.provisioning import tools
from app.agents.provisioning.graph import run_provisioning_agent
from app.agents.provisioning.tools import _strip_fences
from app.db.models import Deployment, User

_VALID_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
spec:
  replicas: 1
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: nginx:1.25
---
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector:
    app: web
  ports:
    - port: 80
"""


# ---------------------------------------------------------------------------
# tools._strip_fences — pure unit tests
# ---------------------------------------------------------------------------


def test_strip_fences_no_fence() -> None:
    assert _strip_fences("apiVersion: v1") == "apiVersion: v1"


def test_strip_fences_yaml_fence() -> None:
    assert _strip_fences("```yaml\napiVersion: v1\n```") == "apiVersion: v1"


def test_strip_fences_unlabeled_fence() -> None:
    assert _strip_fences("```\napiVersion: v1\n```") == "apiVersion: v1"


def test_strip_fences_with_surrounding_whitespace() -> None:
    assert _strip_fences("\n  ```yaml\nfoo: bar\n```  \n") == "foo: bar"


# ---------------------------------------------------------------------------
# Agent — happy path (UC3 / AC #1, #2, #6)
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_user(db_session: Session) -> User:
    """A persisted User row whose id is referenced by Deployment.user_id."""
    from app.core.security import hash_password

    user = User(
        email="agent@example.com",
        hashed_password=hash_password("pw"),
        namespace="user-agent",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


async def test_happy_path_completes_full_pipeline(
    db_session: Session,
    seeded_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_generate(_requirements: str) -> str:
        return _VALID_YAML

    applied: list[tuple[str, str]] = []

    def fake_apply(namespace: str, yaml_str: str) -> list[dict[str, Any]]:
        applied.append((namespace, yaml_str))
        return [{"name": "web", "action": "created"}]

    monkeypatch.setattr(tools, "generate_manifests", fake_generate)
    monkeypatch.setattr(tools, "apply_to_cluster", fake_apply)

    result = await run_provisioning_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        app_name="web",
        requirements="nginx with a service",
        chat_session_id=None,
        db=db_session,
    )

    assert result["status"] == "deployed"
    assert result["deployment_id"] is not None
    assert result["error"] is None

    # AC #2 — DB row created with the right ownership and namespace, and
    # chat_session_id propagated (None for the standalone path).
    deployment = db_session.get(Deployment, result["deployment_id"])
    assert deployment is not None
    assert deployment.user_id == seeded_user.id
    assert deployment.app_name == "web"
    assert deployment.status == "deployed"
    assert deployment.desired_state_yaml == _VALID_YAML
    assert deployment.chat_session_id is None

    # K8s tool was called with the user's namespace and the generated YAML.
    assert applied == [(seeded_user.namespace, _VALID_YAML)]


async def test_chat_session_id_is_persisted_when_provided(
    db_session: Session,
    seeded_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same path, but invoked from chat — chat_session_id must round-trip."""
    from app.db.models import ChatSession

    chat = ChatSession(user_id=seeded_user.id, title="design")
    db_session.add(chat)
    db_session.commit()
    db_session.refresh(chat)

    async def fake_generate(_requirements: str) -> str:
        return _VALID_YAML

    monkeypatch.setattr(tools, "generate_manifests", fake_generate)
    monkeypatch.setattr(
        tools, "apply_to_cluster", lambda ns, y: [{"name": "web", "action": "created"}]
    )

    result = await run_provisioning_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        app_name="web",
        requirements="x",
        chat_session_id=chat.id,
        db=db_session,
    )
    assert result["status"] == "deployed"

    deployment = db_session.get(Deployment, result["deployment_id"])
    assert deployment is not None
    assert deployment.chat_session_id == chat.id


# ---------------------------------------------------------------------------
# Agent — failure paths (AC #3, #4)
# ---------------------------------------------------------------------------


async def test_empty_requirements_returns_failed_with_message(
    db_session: Session, seeded_user: User
) -> None:
    """AC #3: empty requirements -> status='failed' (returned, not raised)."""
    result = await run_provisioning_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        app_name="web",
        requirements="   ",
        db=db_session,
    )
    assert result["status"] == "failed"
    assert result["error"] == "requirements cannot be empty"
    assert result["deployment_id"] is None


async def test_gemini_failure_returns_failed_not_raised(
    db_session: Session,
    seeded_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(_requirements: str) -> str:
        raise RuntimeError("gemini unreachable")

    monkeypatch.setattr(tools, "generate_manifests", boom)

    result = await run_provisioning_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        app_name="web",
        requirements="anything",
        db=db_session,
    )
    assert result["status"] == "failed"
    assert "manifest generation failed" in result["error"]
    # Failure happens before store_in_db -> no row.
    assert result["deployment_id"] is None
    assert db_session.query(Deployment).count() == 0


async def test_invalid_yaml_from_llm_is_rejected(
    db_session: Session,
    seeded_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #4: yaml.safe_load_all gates the apply call."""

    async def fake_generate(_r: str) -> str:
        return "this is: not: valid: yaml: at: all: : :"

    monkeypatch.setattr(tools, "generate_manifests", fake_generate)

    result = await run_provisioning_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        app_name="web",
        requirements="x",
        db=db_session,
    )
    assert result["status"] == "failed"
    assert "invalid" in result["error"].lower()
    assert db_session.query(Deployment).count() == 0


async def test_k8s_apply_failure_marks_row_failed(
    db_session: Session,
    seeded_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When apply_to_cluster fails after the row was stored, status='failed'
    is returned AND the persisted row is updated to status='failed'."""

    async def fake_generate(_r: str) -> str:
        return _VALID_YAML

    def boom_apply(_ns: str, _y: str) -> list[dict[str, Any]]:
        raise RuntimeError("cluster unreachable")

    monkeypatch.setattr(tools, "generate_manifests", fake_generate)
    monkeypatch.setattr(tools, "apply_to_cluster", boom_apply)

    result = await run_provisioning_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        app_name="web",
        requirements="x",
        db=db_session,
    )
    assert result["status"] == "failed"
    assert "k8s apply failed" in result["error"]
    assert result["deployment_id"] is not None  # row was created before apply
    deployment = db_session.get(Deployment, result["deployment_id"])
    assert deployment is not None
    assert deployment.status == "failed"


# ---------------------------------------------------------------------------
# AC #7, #8 — stub gone; POST /deploy round-trips status='deployed'
# ---------------------------------------------------------------------------


def test_run_provisioning_agent_no_longer_raises_not_implemented() -> None:
    """AC #7: importing the agent function does not blow up at import time."""
    from app.agents.provisioning.graph import run_provisioning_agent as fn

    assert callable(fn)


def test_post_deploy_returns_status_deployed_end_to_end(
    client: TestClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #8: full POST /api/v1/deploy/ with all tools mocked succeeds."""

    async def fake_generate(_requirements: str) -> str:
        return _VALID_YAML

    monkeypatch.setattr(tools, "generate_manifests", fake_generate)
    monkeypatch.setattr(
        tools,
        "apply_to_cluster",
        lambda ns, y: [{"name": "web", "action": "created"}],
    )

    resp = client.post(
        "/api/v1/deploy/",
        json={"app_name": "web", "requirements": "nginx with a service"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "deployed"
    assert body["deployment_id"] is not None
    assert body["error"] is None
