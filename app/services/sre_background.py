"""Background SRE Monitor service (spec §9, issue #14).

Long-running asyncio task that periodically scans every user's namespace for
Kubernetes Warning events, runs the SRE agent against affected deployments,
and pushes alerts to active WebSocket sessions.

Lifespan wiring (start/stop in `app/main.py`) is reserved for issue #18; this
module only provides the loop and its helpers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.agents.sre.graph import run_sre_agent
from app.core.config import settings
from app.core.ws_manager import ConnectionManager, manager
from app.db.models import ChatMessage, Deployment, RemediationPlan, User
from app.db.session import SessionLocal
from app.services.k8s_client import KubernetesService

logger = logging.getLogger(__name__)


SessionFactory = Callable[[], Session]


async def sre_background_loop(
    *,
    db_factory: SessionFactory = SessionLocal,
    ws_manager: ConnectionManager = manager,
    k8s_provider: Callable[[], Any] | None = None,
) -> None:
    """Run the SRE scan loop forever until cancelled.

    Each iteration calls `_scan_all_users` and then sleeps for
    `settings.SRE_SCAN_INTERVAL_SECONDS`. `asyncio.CancelledError` propagates
    so FastAPI shutdown can await a clean exit; all other exceptions are
    logged and the loop continues.
    """
    while True:
        try:
            await _scan_all_users(
                db_factory=db_factory,
                ws_manager=ws_manager,
                k8s_provider=k8s_provider,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background scan cycle failed: %s", exc)
        await asyncio.sleep(settings.SRE_SCAN_INTERVAL_SECONDS)


async def _scan_all_users(
    *,
    db_factory: SessionFactory = SessionLocal,
    ws_manager: ConnectionManager = manager,
    k8s_provider: Callable[[], Any] | None = None,
) -> None:
    """One pass over every user with a deployed workload."""
    try:
        k8s = (k8s_provider or KubernetesService.get_instance)()
    except Exception as exc:
        logger.warning("K8s client unavailable; skipping background scan: %s", exc)
        return

    db = db_factory()
    try:
        users = (
            db.query(User)
            .join(Deployment, Deployment.user_id == User.id)
            .filter(Deployment.status == "deployed")
            .distinct()
            .all()
        )
        for user in users:
            try:
                await _scan_user(user=user, db=db, k8s=k8s, ws_manager=ws_manager)
            except Exception as exc:
                logger.warning("Background scan for user %s failed: %s", user.id, exc)
                db.rollback()
    finally:
        db.close()


async def _scan_user(
    *,
    user: User,
    db: Session,
    k8s: Any,
    ws_manager: ConnectionManager,
) -> None:
    try:
        events = k8s.get_warning_events(user.namespace)
    except Exception as exc:
        logger.warning("get_warning_events failed for namespace %s: %s", user.namespace, exc)
        return

    if not events:
        return

    deployments = (
        db.query(Deployment)
        .filter(Deployment.user_id == user.id, Deployment.status == "deployed")
        .all()
    )
    if not deployments:
        return

    affected = _find_affected_deployments(events, deployments)
    if not affected:
        return

    interval = settings.SRE_SCAN_INTERVAL_SECONDS
    now = datetime.now(timezone.utc)

    for deployment in affected:
        latest_unresolved = _latest_unresolved_plan(db, deployment.id)

        if is_recent_unresolved_plan(latest_unresolved, now, interval):
            continue
        if latest_unresolved is not None and events_match(events, latest_unresolved.event_summary):
            continue

        result = await run_sre_agent(
            user_id=user.id,
            namespace=user.namespace,
            deployment_id=deployment.id,
            source="background",
            db=db,
        )

        if result.get("status") != "awaiting_approval" or result.get("plan_db_id") is None:
            continue

        await _maybe_alert_user(
            user=user,
            deployment=deployment,
            result=result,
            db=db,
            ws_manager=ws_manager,
        )


async def _maybe_alert_user(
    *,
    user: User,
    deployment: Deployment,
    result: dict[str, Any],
    db: Session,
    ws_manager: ConnectionManager,
) -> None:
    if not await ws_manager.is_user_connected(user.id):
        return

    session_id = await ws_manager.get_active_session(user.id)
    if session_id is None:
        return

    plan_id: int = result["plan_db_id"]
    analysis = result.get("analysis") or "An issue was detected."
    content = (
        f"SRE alert for '{deployment.app_name}': {analysis}\n\n"
        "A remediation plan is awaiting your approval."
    )
    metadata = json.dumps(
        {
            "deployment_id": deployment.id,
            "plan_id": plan_id,
            "source": "background",
            "status": "awaiting_approval",
        }
    )

    position = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).count()
    chat_msg = ChatMessage(
        session_id=session_id,
        role="sre-agent",
        content=content,
        metadata_json=metadata,
        position=position,
    )
    db.add(chat_msg)
    # NFR §12.7: persist before WS delivery so a disconnect cannot lose the alert.
    db.commit()
    db.refresh(chat_msg)

    await ws_manager.send_to_user(
        user.id,
        {
            "type": "sre_alert",
            "deployment_id": deployment.id,
            "app_name": deployment.app_name,
            "message": {
                "id": chat_msg.id,
                "role": chat_msg.role,
                "content": chat_msg.content,
                "metadata_json": chat_msg.metadata_json,
                "position": chat_msg.position,
                "created_at": chat_msg.created_at.isoformat() if chat_msg.created_at else None,
            },
        },
    )


def _latest_unresolved_plan(db: Session, deployment_id: int) -> RemediationPlan | None:
    return (
        db.query(RemediationPlan)
        .filter(
            RemediationPlan.deployment_id == deployment_id,
            RemediationPlan.approved.is_(False),
            RemediationPlan.applied.is_(False),
        )
        .order_by(RemediationPlan.created_at.desc())
        .first()
    )


def _find_affected_deployments(
    events: list[dict[str, Any]], deployments: list[Deployment]
) -> list[Deployment]:
    """Return deployments whose `app_name` matches any event's involved object.

    Pod names follow `{deployment}-{rs-hash}-{rand}`; ReplicaSet names follow
    `{deployment}-{rs-hash}`; Deployment events use the deployment name itself.
    Matching the prefix covers all three cases.
    """
    affected: list[Deployment] = []
    seen: set[int] = set()
    for ev in events:
        obj = ev.get("involved_object") or {}
        name = obj.get("name") or ""
        if not name:
            continue
        for dep in deployments:
            if dep.id in seen:
                continue
            app = dep.app_name
            if name == app or name.startswith(f"{app}-"):
                affected.append(dep)
                seen.add(dep.id)
    return affected


def is_recent_unresolved_plan(
    plan: RemediationPlan | None, now: datetime, interval_seconds: int
) -> bool:
    """Time-based dedup: True iff `plan` is unresolved and within 2×interval."""
    if plan is None:
        return False
    if plan.approved or plan.applied:
        return False
    plan_time = plan.created_at
    if plan_time is None:
        return False
    if plan_time.tzinfo is None:
        plan_time = plan_time.replace(tzinfo=timezone.utc)
    return (now - plan_time) <= timedelta(seconds=interval_seconds * 2)


def events_match(events: list[dict[str, Any]], prior_event_summary: str) -> bool:
    """Event-based dedup: True iff `events` describe the same issues as `prior`.

    Compares (reason, message, involved_object) tuples; ignores `count` and
    `last_timestamp` since those churn on every poll.
    """
    if not prior_event_summary:
        return False
    try:
        prior = json.loads(prior_event_summary)
    except (TypeError, ValueError):
        return False
    if not isinstance(prior, list):
        return False
    return _normalize(events) == _normalize(prior)


def _normalize(events: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return sorted(
        (
            ev.get("reason"),
            ev.get("message"),
            (ev.get("involved_object") or {}).get("kind"),
            (ev.get("involved_object") or {}).get("name"),
            (ev.get("involved_object") or {}).get("namespace"),
        )
        for ev in events
        if isinstance(ev, dict)
    )
