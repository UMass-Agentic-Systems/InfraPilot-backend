"""Pure unit tests for the SRE background monitor dedup helpers (issue #15, UC20).

Targets two functions in `app.services.sre_background`:

* `is_recent_unresolved_plan(plan, now, interval)` — time-window dedup
* `events_match(events, prior_event_summary)` — event-tuple dedup
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Deployment, RemediationPlan, User
from app.services.sre_background import events_match, is_recent_unresolved_plan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_user(db: Session, email: str = "u@example.com", namespace: str = "user-u") -> User:
    user = User(email=email, hashed_password="hash", namespace=namespace)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_deployment(db: Session, user_id: int) -> Deployment:
    deployment = Deployment(
        user_id=user_id,
        app_name="web",
        desired_state_yaml="",
        status="deployed",
    )
    db.add(deployment)
    db.commit()
    db.refresh(deployment)
    return deployment


def _seed_plan(
    db: Session,
    deployment_id: int,
    *,
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
        source="background",
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


# ---------------------------------------------------------------------------
# is_recent_unresolved_plan
# ---------------------------------------------------------------------------


def test_is_recent_unresolved_plan_handles_none() -> None:
    assert is_recent_unresolved_plan(None, datetime.now(timezone.utc), 120) is False


def test_is_recent_unresolved_plan_false_when_approved_or_applied(
    db_session: Session,
) -> None:
    user = _seed_user(db_session)
    dep = _seed_deployment(db_session, user.id)
    now = datetime.now(timezone.utc)

    approved = _seed_plan(db_session, dep.id, approved=True, created_at=now)
    applied = _seed_plan(db_session, dep.id, approved=True, applied=True, created_at=now)

    assert is_recent_unresolved_plan(approved, now, 120) is False
    assert is_recent_unresolved_plan(applied, now, 120) is False


def test_is_recent_unresolved_plan_true_within_window(db_session: Session) -> None:
    """Plans created within `2 × interval` count as recent."""
    user = _seed_user(db_session)
    dep = _seed_deployment(db_session, user.id)
    now = datetime.now(timezone.utc)

    plan = _seed_plan(db_session, dep.id, created_at=now - timedelta(seconds=60))
    assert is_recent_unresolved_plan(plan, now, 120) is True


def test_is_recent_unresolved_plan_false_outside_window(db_session: Session) -> None:
    """Plans older than `2 × interval` are not recent and should not dedup."""
    user = _seed_user(db_session)
    dep = _seed_deployment(db_session, user.id)
    now = datetime.now(timezone.utc)

    plan = _seed_plan(db_session, dep.id, created_at=now - timedelta(seconds=500))
    assert is_recent_unresolved_plan(plan, now, 120) is False


# ---------------------------------------------------------------------------
# events_match
# ---------------------------------------------------------------------------


def test_events_match_identical_unordered() -> None:
    a = [_POD_EVENT, dict(_POD_EVENT, reason="OOMKilled")]
    b = [dict(_POD_EVENT, reason="OOMKilled"), _POD_EVENT]
    assert events_match(a, json.dumps(b)) is True


def test_events_match_ignores_count_and_timestamp() -> None:
    prior = json.dumps([dict(_POD_EVENT, count=99, last_timestamp="2020-01-01T00:00:00")])
    assert events_match([_POD_EVENT], prior) is True


def test_events_match_false_on_different_reason() -> None:
    prior = json.dumps([dict(_POD_EVENT, reason="OOMKilled")])
    assert events_match([_POD_EVENT], prior) is False


def test_events_match_false_on_invalid_json() -> None:
    assert events_match([_POD_EVENT], "not-json") is False
    assert events_match([_POD_EVENT], "") is False
