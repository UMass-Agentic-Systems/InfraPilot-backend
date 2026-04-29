"""Smoke tests for the cross-cutting schemas package (issue #9, AC #4).

These tests verify that:
- The package is importable both as a top-level namespace and as submodules.
- Each declared model can roundtrip through `model_dump()` / re-validate.
"""

import pytest
from pydantic import ValidationError

from app.api import schemas
from app.api.schemas import (
    ApproveRequest,
    ApproveResponse,
    DeployRequest,
    DeployResponse,
    MessageOut,
    PlanSummary,
    RegisterRequest,
    ScanRequest,
    ScanResponse,
    SessionCreate,
    SessionDetail,
    TokenResponse,
    VisualizeResponse,
    WSAgentResponse,
    WSClientMessage,
    WSError,
    WSSREAlert,
    WSTyping,
)
from app.api.schemas.auth import RegisterRequest as RegisterRequestDirect


def test_subpackage_direct_import_works() -> None:
    """AC #4 — `from app.api.schemas.auth import RegisterRequest` works."""
    assert RegisterRequestDirect is RegisterRequest


def test_top_level_reexports_cover_each_domain() -> None:
    for name in (
        "RegisterRequest",
        "DeployRequest",
        "ScanRequest",
        "SessionCreate",
        "VisualizeResponse",
        "WSAgentResponse",
    ):
        assert hasattr(schemas, name), f"missing re-export: {name}"


def test_token_response_default_token_type() -> None:
    t = TokenResponse(access_token="abc")
    assert t.token_type == "bearer"


def test_token_response_rejects_other_token_types() -> None:
    with pytest.raises(ValidationError):
        TokenResponse(access_token="abc", token_type="basic")  # type: ignore[arg-type]


def test_deploy_request_requires_app_name() -> None:
    with pytest.raises(ValidationError):
        DeployRequest(app_name="", requirements="x")


def test_deploy_response_optional_fields_default_none() -> None:
    r = DeployResponse(status="failed")
    assert r.deployment_id is None
    assert r.error is None


def test_scan_request_and_response_roundtrip() -> None:
    req = ScanRequest(deployment_id=42)
    assert req.deployment_id == 42
    resp = ScanResponse(status="awaiting_approval", plan_db_id=7, analysis="x", rationale="y")
    assert resp.model_dump()["plan_db_id"] == 7


def test_approve_request_and_response() -> None:
    ApproveRequest(approved=True)
    ApproveResponse(plan_id=1, approved=True, applied=False)


def test_session_create_default_title() -> None:
    assert SessionCreate().title == "New Chat"


def test_session_detail_defaults_to_empty_messages() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    detail = SessionDetail(id=1, title="t", created_at=now, updated_at=now)
    assert detail.messages == []


def test_message_out_validates_role() -> None:
    from datetime import datetime, timezone

    MessageOut(
        id=1,
        role="infra-agent",
        content="hi",
        position=0,
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(ValidationError):
        MessageOut(
            id=1,
            role="assistant",  # type: ignore[arg-type]
            content="hi",
            position=0,
            created_at=datetime.now(timezone.utc),
        )


def test_ws_frame_models_have_literal_type_discriminator() -> None:
    assert WSClientMessage(content="hi").type == "message"
    assert WSTyping(agent="sre-agent", is_typing=True).type == "typing"
    assert WSError(detail="boom").type == "error"


def test_ws_agent_response_and_alert_carry_message() -> None:
    from datetime import datetime, timezone

    msg = MessageOut(
        id=1,
        role="sre-agent",
        content="alert!",
        metadata_json='{"plan_id": 7}',
        position=0,
        created_at=datetime.now(timezone.utc),
    )
    WSAgentResponse(message=msg)
    WSSREAlert(deployment_id=1, app_name="api", message=msg)


def test_plan_summary_from_attributes_supported() -> None:
    """PlanSummary should be constructible from an ORM-like object via from_attributes."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    ns = SimpleNamespace(
        id=1,
        deployment_id=2,
        analysis="a",
        rationale="r",
        approved=False,
        applied=False,
        source="manual",
        created_at=datetime.now(timezone.utc),
    )
    summary = PlanSummary.model_validate(ns)
    assert summary.id == 1


def test_visualize_response_minimal_fields_only() -> None:
    """When the cluster is unreachable, only DB fields are populated (spec §6.5)."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    v = VisualizeResponse(
        deployment_id=1,
        app_name="api",
        status="deployed",
        namespace="user-x",
        created_at=now,
        updated_at=now,
        error="cluster unreachable",
    )
    assert v.cluster is None
    assert v.tiers == []
    assert v.traffic is None
    assert v.error == "cluster unreachable"
