"""Chat session REST + WebSocket endpoints (spec §6.4, issues #16 & #17)."""

import logging
import re
from datetime import datetime, timezone

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from jose import JWTError
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.agents.provisioning.graph import run_provisioning_agent
from app.agents.sre.graph import run_sre_agent
from app.api.deps import get_current_user, get_db
from app.services.k8s_client import KubernetesService
from app.api.schemas.chat import (
    AgentName,
    DeploymentSummary,
    MessageOut,
    SessionCreate,
    SessionDetail,
    SessionSummary,
    WSAgentResponse,
    WSClientMessage,
    WSError,
    WSTyping,
)
from app.core.security import decode_access_token
from app.core.ws_manager import manager
from app.db.models import ChatMessage, ChatSession, Deployment, User

logger = logging.getLogger(__name__)
router = APIRouter()

_SESSION_NOT_FOUND = "Session not found"
_WS_SESSION_DELETED = 4010
_WS_AUTH_FAILED = 4001
_WS_FORBIDDEN = 4004

_DEPLOY_RE = re.compile(r"\b(?:provision|deploy|launch|spin\s+up|install)\b", re.IGNORECASE)
_DELETE_RE = re.compile(
    r"\b(?:delete|destroy|teardown|tear\s+down|uninstall|remove)\b", re.IGNORECASE
)
_SRE_KEYWORDS = ("scan", "monitor", "check", "fix", "remediate", "approve")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_intent(content: str) -> str:
    text = content.lower()
    # Check delete first — "remove" is in the delete set and would otherwise be
    # ambiguous against future deploy variants. Delete keywords don't overlap
    # with the deploy set today.
    if _DELETE_RE.search(text):
        return "delete"
    if _DEPLOY_RE.search(text):
        return "deploy"
    if any(kw in text for kw in _SRE_KEYWORDS):
        return "sre"
    return "general"


def _delete_deployment(
    deployment: Deployment, namespace: str, db: Session
) -> tuple[int, list[str]]:
    """Tear down the deployment's K8s resources and drop the DB row.

    Returns (deleted_count, errors). RemediationPlan rows CASCADE on delete.
    K8s failures per resource are isolated — we report them but still remove
    the DB row so the user isn't left with a phantom deployment.
    """
    deleted = 0
    errors: list[str] = []
    yaml_str = deployment.desired_state_yaml or ""
    if yaml_str.strip():
        try:
            k8s = KubernetesService.get_instance()
        except Exception as exc:  # K8s unreachable
            errors.append(f"Kubernetes unavailable: {exc}")
        else:
            for doc in yaml.safe_load_all(yaml_str):
                if not isinstance(doc, dict):
                    continue
                kind = doc.get("kind")
                name = (doc.get("metadata") or {}).get("name")
                if not kind or not name:
                    continue
                try:
                    k8s.delete_resource(namespace, kind, name)
                    deleted += 1
                except Exception as exc:
                    errors.append(f"{kind}/{name}: {exc}")

    db.delete(deployment)
    db.commit()
    return deleted, errors


def _extract_app_name(content: str) -> str:
    match = re.search(
        r"(?:deploy|provision|install|launch)\s+(?:a\s+|an\s+|my\s+)?(\S+)",
        content.lower(),
    )
    if match:
        raw = match.group(1)
        name = re.sub(r"[^a-z0-9-]", "-", raw).strip("-")[:63]
        if name and name[0].isalpha():
            return name
    return "my-app"


def _next_position(db: Session, session_id: int) -> int:
    result = (
        db.query(func.max(ChatMessage.position))
        .filter(ChatMessage.session_id == session_id)
        .scalar()
    )
    return (result if result is not None else -1) + 1


async def _dispatch(
    *,
    intent: str,
    content: str,
    user: User,
    session_id: int,
    db: Session,
) -> str:
    if intent == "deploy":
        app_name = _extract_app_name(content)
        try:
            result = await run_provisioning_agent(
                user_id=user.id,
                namespace=user.namespace,
                app_name=app_name,
                requirements=content,
                chat_session_id=session_id,
                db=db,
            )
        except Exception as exc:
            return f"Deployment failed: {exc}"
        if result["status"] == "failed":
            return f"Deployment failed: {result.get('error', 'unknown error')}"
        return (
            f"Deployed successfully — app: {app_name}, "
            f"deployment ID: {result['deployment_id']}, status: {result['status']}"
        )

    if intent == "delete":
        # Scope to deployments created in *this* session — same isolation rule
        # the visualize endpoint enforces. Most-recent first so phrasing like
        # "delete my app" targets what the user just deployed.
        deployment = (
            db.query(Deployment)
            .filter(Deployment.user_id == user.id, Deployment.chat_session_id == session_id)
            .order_by(Deployment.created_at.desc())
            .first()
        )
        if deployment is None:
            return "No deployments found in this session to delete."
        app_name = deployment.app_name
        deployment_id = deployment.id
        try:
            deleted, errors = _delete_deployment(deployment, user.namespace, db)
        except Exception as exc:
            return f"Deletion failed: {exc}"
        if errors:
            return (
                f"Deleted deployment ID: {deployment_id}, app: {app_name}, status: deleted "
                f"(removed {deleted} K8s resource(s); errors: {'; '.join(errors)})"
            )
        return (
            f"Deleted deployment ID: {deployment_id}, app: {app_name}, status: deleted "
            f"(removed {deleted} K8s resource(s))"
        )

    if intent == "sre":
        deployment = (
            db.query(Deployment)
            .filter(Deployment.user_id == user.id)
            .order_by(Deployment.created_at.desc())
            .first()
        )
        if deployment is None:
            return "No deployments found. Deploy an application first before running a scan."
        try:
            result = await run_sre_agent(
                user_id=user.id,
                namespace=user.namespace,
                deployment_id=deployment.id,
                source="manual",
                db=db,
            )
        except Exception as exc:
            return f"SRE scan failed: {exc}"
        if result["status"] == "no_issues":
            return f"SRE scan complete — no issues found in deployment {deployment.id}."
        return (
            f"SRE scan complete — {result.get('analysis', '')}. "
            f"Plan ID: {result.get('plan_db_id')}, status: {result['status']}"
        )

    return (
        "I can help you deploy applications or monitor your infrastructure. "
        "Try: 'deploy my web app' or 'scan my deployment'."
    )


# ---------------------------------------------------------------------------
# REST endpoints (issue #16)
# ---------------------------------------------------------------------------


@router.post("/sessions", response_model=SessionSummary, status_code=status.HTTP_201_CREATED)
def create_session(
    payload: SessionCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatSession:
    session = ChatSession(user_id=user.id, title=payload.title)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


@router.get("/sessions", response_model=list[SessionSummary])
def list_sessions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ChatSession]:
    return (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc())
        .all()
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
def get_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SessionDetail:
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SESSION_NOT_FOUND)

    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.position)
        .limit(200)
        .all()
    )

    return SessionDetail(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        messages=[MessageOut.model_validate(m) for m in messages],
    )


@router.get(
    "/sessions/{session_id}/deployments",
    response_model=list[DeploymentSummary],
)
def list_session_deployments(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Deployment]:
    """Authoritative list of deployments created in this chat session.

    The frontend uses this to populate the Visualization tab's deployment
    dropdown so it survives logout/login (chat-bubble regex scraping was
    unreliable). Standalone deploys (chat_session_id IS NULL) are intentionally
    excluded — they're not session-scoped.
    """
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SESSION_NOT_FOUND)

    return (
        db.query(Deployment)
        .filter(
            Deployment.user_id == user.id,
            Deployment.chat_session_id == session_id,
        )
        .order_by(Deployment.created_at.desc())
        .all()
    )


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_SESSION_NOT_FOUND)

    await manager.close_session(user.id, session_id, _WS_SESSION_DELETED)

    db.delete(session)
    db.commit()


# ---------------------------------------------------------------------------
# WebSocket endpoint (issue #17)
# ---------------------------------------------------------------------------


@router.websocket("/sessions/{session_id}/ws")
async def chat_ws(
    session_id: int,
    websocket: WebSocket,
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> None:
    # Validate JWT from query parameter
    try:
        payload = decode_access_token(token)
        user_id = int(payload.get("sub", ""))
    except (JWTError, ValueError, TypeError):
        await websocket.close(code=_WS_AUTH_FAILED)
        return

    user = db.get(User, user_id)
    if user is None:
        await websocket.close(code=_WS_AUTH_FAILED)
        return

    # Verify session ownership
    session = db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        await websocket.close(code=_WS_FORBIDDEN)
        return

    # All checks pass — register connection and enter message loop
    await manager.connect(user.id, session_id, websocket)

    try:
        while True:
            try:
                raw = await websocket.receive_json()
                client_msg = WSClientMessage.model_validate(raw)
            except WebSocketDisconnect:
                raise
            except Exception:
                await websocket.send_json(WSError(detail="Invalid message format").model_dump())
                continue

            content = client_msg.content
            intent = _detect_intent(content)
            agent_name: AgentName = "sre-agent" if intent == "sre" else "infra-agent"

            # Persist user message before agent invocation
            user_pos = _next_position(db, session_id)
            user_msg = ChatMessage(
                session_id=session_id, role="user", content=content, position=user_pos
            )
            db.add(user_msg)
            db.commit()
            db.refresh(user_msg)

            # Typing indicator — start
            await websocket.send_json(WSTyping(agent=agent_name, is_typing=True).model_dump())

            # Route to agent
            response_content = await _dispatch(
                intent=intent,
                content=content,
                user=user,
                session_id=session_id,
                db=db,
            )

            # Persist agent response before sending to client
            agent_pos = _next_position(db, session_id)
            agent_msg = ChatMessage(
                session_id=session_id,
                role=agent_name,
                content=response_content,
                position=agent_pos,
            )
            db.add(agent_msg)
            db.commit()
            db.refresh(agent_msg)

            # Update session timestamp
            session.updated_at = datetime.now(timezone.utc)
            db.commit()

            # Completion indicator then response
            await websocket.send_json(WSTyping(agent=agent_name, is_typing=False).model_dump())
            await websocket.send_json(
                WSAgentResponse(message=MessageOut.model_validate(agent_msg)).model_dump(
                    mode="json"
                )
            )

    except WebSocketDisconnect:
        pass
    finally:
        await manager.disconnect(user.id, session_id)
