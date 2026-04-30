"""Tests for the SRE LangGraph agent (issue #12)."""

import json
from typing import Any

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agents.sre import graph as sre_graph
from app.agents.sre import tools
from app.agents.sre.graph import resume_sre_agent, run_sre_agent
from app.db.models import Deployment, RemediationPlan, User


@pytest.fixture(autouse=True)
def _reset_checkpointer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The graph's checkpointer is module-scoped (it must outlive a single
    request so resume can find the paused state). Tests reuse the same
    in-memory thread_ids, so we install a fresh MemorySaver per test to keep
    state from leaking between cases."""
    monkeypatch.setattr(sre_graph, "_CHECKPOINTER", MemorySaver())


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_user(db_session: Session) -> User:
    from app.core.security import hash_password

    user = User(
        email="sre@example.com",
        hashed_password=hash_password("pw"),
        namespace="user-sre",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def seeded_deployment(db_session: Session, seeded_user: User) -> Deployment:
    deployment = Deployment(
        user_id=seeded_user.id,
        app_name="web",
        desired_state_yaml="",
        status="deployed",
    )
    db_session.add(deployment)
    db_session.commit()
    db_session.refresh(deployment)
    return deployment


_SAMPLE_EVENTS: list[dict[str, Any]] = [
    {
        "reason": "BackOff",
        "message": "Back-off restarting failed container",
        "involved_object": {"kind": "Pod", "name": "web-abc", "namespace": "user-sre"},
        "count": 5,
        "last_timestamp": "2025-01-01T00:00:00",
    }
]

_SAMPLE_ANALYSIS = {
    "analysis": "Container is crash-looping due to bad config",
    "remediation_plan": {
        "actions": [
            {"type": "restart_deployment", "target": "web", "params": {}},
        ]
    },
    "rationale": "Restart often clears transient crash loops",
}


def _patch_llm(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any] | None = None) -> None:
    payload = response if response is not None else _SAMPLE_ANALYSIS

    async def fake_analyze(_events_json: str) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(tools, "analyze_events", fake_analyze)


def _patch_events(monkeypatch: pytest.MonkeyPatch, events: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(tools, "fetch_warning_events", lambda _ns: events)


# ---------------------------------------------------------------------------
# poll_events short-circuit
# ---------------------------------------------------------------------------


async def test_no_warning_events_returns_no_issues(
    db_session: Session,
    seeded_user: User,
    seeded_deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_events(monkeypatch, [])

    result = await run_sre_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        deployment_id=seeded_deployment.id,
        db=db_session,
    )

    assert result["status"] == "no_issues"
    assert result["plan_db_id"] is None
    assert db_session.query(RemediationPlan).count() == 0


# ---------------------------------------------------------------------------
# Happy path: run pauses at interrupt, plan persisted
# ---------------------------------------------------------------------------


async def test_happy_path_creates_plan_and_pauses(
    db_session: Session,
    seeded_user: User,
    seeded_deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_events(monkeypatch, _SAMPLE_EVENTS)
    _patch_llm(monkeypatch)

    result = await run_sre_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        deployment_id=seeded_deployment.id,
        source="manual",
        db=db_session,
    )

    assert result["status"] == "awaiting_approval"
    assert result["plan_db_id"] is not None
    assert result["analysis"] == _SAMPLE_ANALYSIS["analysis"]
    assert result["rationale"] == _SAMPLE_ANALYSIS["rationale"]

    plan = db_session.get(RemediationPlan, result["plan_db_id"])
    assert plan is not None
    assert plan.deployment_id == seeded_deployment.id
    assert plan.approved is False
    assert plan.applied is False
    assert plan.source == "manual"
    assert plan.analysis == _SAMPLE_ANALYSIS["analysis"]
    assert plan.rationale == _SAMPLE_ANALYSIS["rationale"]
    assert json.loads(plan.event_summary) == _SAMPLE_EVENTS
    assert json.loads(plan.plan_json) == _SAMPLE_ANALYSIS["remediation_plan"]


async def test_source_background_is_persisted(
    db_session: Session,
    seeded_user: User,
    seeded_deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_events(monkeypatch, _SAMPLE_EVENTS)
    _patch_llm(monkeypatch)

    result = await run_sre_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        deployment_id=seeded_deployment.id,
        source="background",
        db=db_session,
    )

    plan = db_session.get(RemediationPlan, result["plan_db_id"])
    assert plan is not None
    assert plan.source == "background"


# ---------------------------------------------------------------------------
# Resume: approval -> apply, rejection -> skip
# ---------------------------------------------------------------------------


async def test_resume_with_approval_applies_and_marks_applied(
    db_session: Session,
    seeded_user: User,
    seeded_deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_events(monkeypatch, _SAMPLE_EVENTS)
    _patch_llm(monkeypatch)

    applied_calls: list[tuple[str, str]] = []

    def fake_apply(namespace: str, plan_json: str) -> dict[str, Any]:
        applied_calls.append((namespace, plan_json))
        return {"results": [{"type": "restart_deployment", "target": "web", "status": "ok"}]}

    monkeypatch.setattr(tools, "apply_remediation", fake_apply)

    run_result = await run_sre_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        deployment_id=seeded_deployment.id,
        db=db_session,
    )
    assert run_result["status"] == "awaiting_approval"

    resume_result = await resume_sre_agent(
        user_id=seeded_user.id,
        deployment_id=seeded_deployment.id,
        approved=True,
        db=db_session,
    )

    assert resume_result["plan_id"] == run_result["plan_db_id"]
    assert resume_result["approved"] is True
    assert resume_result["applied"] is True
    assert applied_calls and applied_calls[0][0] == seeded_user.namespace

    db_session.expire_all()
    plan = db_session.get(RemediationPlan, run_result["plan_db_id"])
    assert plan is not None
    assert plan.approved is True
    assert plan.applied is True


async def test_resume_with_rejection_skips_apply(
    db_session: Session,
    seeded_user: User,
    seeded_deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_events(monkeypatch, _SAMPLE_EVENTS)
    _patch_llm(monkeypatch)

    apply_calls: list[Any] = []

    def fake_apply(namespace: str, plan_json: str) -> dict[str, Any]:
        apply_calls.append((namespace, plan_json))
        return {"results": []}

    monkeypatch.setattr(tools, "apply_remediation", fake_apply)

    run_result = await run_sre_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        deployment_id=seeded_deployment.id,
        db=db_session,
    )

    resume_result = await resume_sre_agent(
        user_id=seeded_user.id,
        deployment_id=seeded_deployment.id,
        approved=False,
        db=db_session,
    )

    assert resume_result["approved"] is False
    assert resume_result["applied"] is False
    assert apply_calls == []

    db_session.expire_all()
    plan = db_session.get(RemediationPlan, run_result["plan_db_id"])
    assert plan is not None
    assert plan.approved is False
    assert plan.applied is False


# ---------------------------------------------------------------------------
# DB CHECK constraint (NFR §12.1) — applied=True only when approved=True
# ---------------------------------------------------------------------------


def test_applied_only_when_approved_db_constraint(
    db_session: Session,
    seeded_deployment: Deployment,
) -> None:
    bad = RemediationPlan(
        deployment_id=seeded_deployment.id,
        event_summary="[]",
        analysis="x",
        plan_json="{}",
        rationale="x",
        approved=False,
        applied=True,
        source="manual",
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Plan rows always have populated analysis + rationale (AC #4)
# ---------------------------------------------------------------------------


async def test_plan_row_has_populated_analysis_and_rationale_via_fallback(
    db_session: Session,
    seeded_user: User,
    seeded_deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM returns garbage, analyze_events still fills both fields."""
    _patch_events(monkeypatch, _SAMPLE_EVENTS)

    # Drive the real fallback path inside analyze_events instead of stubbing it.
    class _FakeResp:
        content = "this is definitely not json"

    class _FakeLLM:
        async def ainvoke(self, _prompt: str) -> _FakeResp:
            return _FakeResp()

    monkeypatch.setattr(tools, "_make_llm", lambda: _FakeLLM())

    result = await run_sre_agent(
        user_id=seeded_user.id,
        namespace=seeded_user.namespace,
        deployment_id=seeded_deployment.id,
        db=db_session,
    )
    assert result["status"] == "awaiting_approval"

    plan = db_session.get(RemediationPlan, result["plan_db_id"])
    assert plan is not None
    assert plan.analysis.strip() != ""
    assert plan.rationale.strip() != ""


# ---------------------------------------------------------------------------
# Per-action failure isolation (AC #5)
# ---------------------------------------------------------------------------


def test_per_action_failure_does_not_abort_remaining_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import k8s_client as k8s_client_module

    fake_k8s: Any = type("FakeK8s", (), {})()
    restart_calls: list[str] = []
    delete_calls: list[tuple[str, str]] = []

    def restart(_ns: str, name: str) -> None:
        restart_calls.append(name)
        if name == "first":
            raise RuntimeError("boom")

    def delete(_ns: str, kind: str, name: str) -> None:
        delete_calls.append((kind, name))

    fake_k8s.restart_deployment = restart
    fake_k8s.delete_resource = delete

    monkeypatch.setattr(
        k8s_client_module.KubernetesService,
        "get_instance",
        classmethod(lambda cls: fake_k8s),
    )

    plan = {
        "actions": [
            {"type": "restart_deployment", "target": "first", "params": {}},
            {"type": "delete_pod", "target": "second-pod", "params": {}},
            {"type": "restart_deployment", "target": "third", "params": {}},
        ]
    }
    out = tools.apply_remediation("user-sre", json.dumps(plan))

    statuses = [r["status"] for r in out["results"]]
    assert statuses == ["failed", "ok", "ok"]
    # Action 1 attempted, action 2 ran (delete_pod), action 3 also ran.
    assert restart_calls == ["first", "third"]
    assert delete_calls == [("Pod", "second-pod")]


# ---------------------------------------------------------------------------
# analyze_events fallback unit test
# ---------------------------------------------------------------------------


async def test_analyze_events_falls_back_on_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        content = "totally not json"

    class _LLM:
        async def ainvoke(self, _p: str) -> _Resp:
            return _Resp()

    monkeypatch.setattr(tools, "_make_llm", lambda: _LLM())

    out = await tools.analyze_events("[]")
    assert out["analysis"]
    assert out["remediation_plan"] == {"actions": []}
    assert "manual review" in out["rationale"].lower()


# ---------------------------------------------------------------------------
# apply_remediation skips unsupported action types
# ---------------------------------------------------------------------------


def test_apply_remediation_skips_unsupported_action_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import k8s_client as k8s_client_module

    monkeypatch.setattr(
        k8s_client_module.KubernetesService,
        "get_instance",
        classmethod(lambda cls: type("K", (), {})()),
    )

    plan = {"actions": [{"type": "format_disk", "target": "node-1", "params": {}}]}
    out = tools.apply_remediation("user-sre", json.dumps(plan))
    assert out["results"][0]["status"] == "skipped"
