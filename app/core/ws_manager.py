import asyncio
import logging
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

_GOING_AWAY = 1001


class ConnectionManager:
    """Tracks active WebSocket connections keyed by (user_id, session_id).

    Used by the chat WebSocket handler and the background SRE monitor to
    deliver messages and alerts to connected users.
    """

    def __init__(self) -> None:
        self.active_connections: dict[int, dict[int, WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: int, session_id: int, websocket: WebSocket) -> None:
        await websocket.accept()
        old: WebSocket | None = None
        async with self._lock:
            user_sessions = self.active_connections.setdefault(user_id, {})
            old = user_sessions.pop(session_id, None)
            # Re-insert at the tail so dict-insertion-order tracks recency.
            user_sessions[session_id] = websocket

        if old is not None:
            try:
                await old.close(code=_GOING_AWAY)
            except Exception as exc:  # pragma: no cover — best-effort cleanup
                logger.debug(
                    "Closing displaced ws for (user=%s, session=%s) failed: %s",
                    user_id,
                    session_id,
                    exc,
                )

    async def disconnect(self, user_id: int, session_id: int) -> None:
        async with self._lock:
            user_sessions = self.active_connections.get(user_id)
            if user_sessions is None:
                return
            user_sessions.pop(session_id, None)
            if not user_sessions:
                self.active_connections.pop(user_id, None)

    async def send_to_session(self, user_id: int, session_id: int, data: dict[str, Any]) -> None:
        async with self._lock:
            ws = self.active_connections.get(user_id, {}).get(session_id)
        if ws is None:
            return
        try:
            await ws.send_json(data)
        except WebSocketDisconnect:
            await self.disconnect(user_id, session_id)
        except Exception as exc:
            logger.warning(
                "send_to_session(user=%s, session=%s) failed: %s",
                user_id,
                session_id,
                exc,
            )
            await self.disconnect(user_id, session_id)

    async def send_to_user(self, user_id: int, data: dict[str, Any]) -> None:
        async with self._lock:
            sessions = list(self.active_connections.get(user_id, {}).items())
        for session_id, ws in sessions:
            try:
                await ws.send_json(data)
            except WebSocketDisconnect:
                await self.disconnect(user_id, session_id)
            except Exception as exc:
                logger.warning(
                    "send_to_user(user=%s, session=%s) failed: %s",
                    user_id,
                    session_id,
                    exc,
                )
                await self.disconnect(user_id, session_id)

    async def get_active_session(self, user_id: int) -> int | None:
        async with self._lock:
            sessions = self.active_connections.get(user_id)
            if not sessions:
                return None
            # Most recently connected = last-inserted key (insertion order).
            return next(reversed(sessions))

    async def is_user_connected(self, user_id: int) -> bool:
        async with self._lock:
            return bool(self.active_connections.get(user_id))


manager = ConnectionManager()
