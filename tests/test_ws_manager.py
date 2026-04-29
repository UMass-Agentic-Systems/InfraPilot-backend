import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import WebSocketDisconnect

from app.core.ws_manager import ConnectionManager, manager


def _fake_ws() -> AsyncMock:
    """Mock WebSocket exposing the async surface ConnectionManager calls."""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    return ws


# ---------------------------------------------------------------------------
# connect / disconnect
# ---------------------------------------------------------------------------


async def test_connect_accepts_and_registers() -> None:
    cm = ConnectionManager()
    ws = _fake_ws()

    await cm.connect(user_id=1, session_id=10, websocket=ws)

    ws.accept.assert_awaited_once()
    assert cm.active_connections[1][10] is ws


async def test_connect_displaces_old_socket_with_1001() -> None:
    cm = ConnectionManager()
    old, new = _fake_ws(), _fake_ws()

    await cm.connect(1, 10, old)
    await cm.connect(1, 10, new)

    old.close.assert_awaited_once_with(code=1001)
    new.close.assert_not_awaited()
    assert cm.active_connections[1][10] is new


async def test_disconnect_removes_session() -> None:
    cm = ConnectionManager()
    ws = _fake_ws()
    await cm.connect(1, 10, ws)

    await cm.disconnect(1, 10)

    assert 1 not in cm.active_connections


async def test_disconnect_keeps_other_sessions_for_same_user() -> None:
    cm = ConnectionManager()
    ws_a, ws_b = _fake_ws(), _fake_ws()
    await cm.connect(1, 10, ws_a)
    await cm.connect(1, 11, ws_b)

    await cm.disconnect(1, 10)

    assert 1 in cm.active_connections
    assert 10 not in cm.active_connections[1]
    assert cm.active_connections[1][11] is ws_b


async def test_disconnect_unknown_is_noop() -> None:
    cm = ConnectionManager()
    # Must not raise.
    await cm.disconnect(user_id=42, session_id=99)
    assert cm.active_connections == {}


# ---------------------------------------------------------------------------
# send_to_session
# ---------------------------------------------------------------------------


async def test_send_to_session_delivers_payload() -> None:
    cm = ConnectionManager()
    ws = _fake_ws()
    await cm.connect(1, 10, ws)

    payload: dict[str, Any] = {"type": "ping"}
    await cm.send_to_session(1, 10, payload)

    ws.send_json.assert_awaited_once_with(payload)


async def test_send_to_session_unknown_is_noop() -> None:
    cm = ConnectionManager()
    # No connection registered — must not raise.
    await cm.send_to_session(user_id=1, session_id=10, data={"type": "ping"})


async def test_send_to_session_disconnect_cleans_up() -> None:
    cm = ConnectionManager()
    ws = _fake_ws()
    ws.send_json = AsyncMock(side_effect=WebSocketDisconnect())
    await cm.connect(1, 10, ws)

    await cm.send_to_session(1, 10, {"type": "ping"})

    assert 1 not in cm.active_connections


# ---------------------------------------------------------------------------
# send_to_user (broadcast across all of a user's sessions)
# ---------------------------------------------------------------------------


async def test_send_to_user_fans_out_to_all_sessions() -> None:
    cm = ConnectionManager()
    ws_a, ws_b = _fake_ws(), _fake_ws()
    await cm.connect(1, 10, ws_a)
    await cm.connect(1, 11, ws_b)

    payload: dict[str, Any] = {"type": "sre_alert"}
    await cm.send_to_user(1, payload)

    ws_a.send_json.assert_awaited_once_with(payload)
    ws_b.send_json.assert_awaited_once_with(payload)


async def test_send_to_user_isolates_other_users() -> None:
    cm = ConnectionManager()
    ws_self, ws_other = _fake_ws(), _fake_ws()
    await cm.connect(1, 10, ws_self)
    await cm.connect(2, 20, ws_other)

    await cm.send_to_user(1, {"type": "ping"})

    ws_self.send_json.assert_awaited_once()
    ws_other.send_json.assert_not_awaited()


async def test_send_to_user_with_no_sessions_is_noop() -> None:
    cm = ConnectionManager()
    # Must not raise.
    await cm.send_to_user(user_id=1, data={"type": "ping"})


async def test_send_to_user_continues_after_one_socket_fails() -> None:
    cm = ConnectionManager()
    ws_dead, ws_live = _fake_ws(), _fake_ws()
    ws_dead.send_json = AsyncMock(side_effect=WebSocketDisconnect())
    await cm.connect(1, 10, ws_dead)
    await cm.connect(1, 11, ws_live)

    await cm.send_to_user(1, {"type": "ping"})

    ws_live.send_json.assert_awaited_once()
    # The dead socket's session was cleaned up; the live one remains.
    assert 10 not in cm.active_connections.get(1, {})
    assert cm.active_connections[1][11] is ws_live


# ---------------------------------------------------------------------------
# get_active_session / is_user_connected
# ---------------------------------------------------------------------------


async def test_get_active_session_returns_most_recently_connected() -> None:
    cm = ConnectionManager()
    await cm.connect(1, 10, _fake_ws())
    await cm.connect(1, 11, _fake_ws())
    await cm.connect(1, 12, _fake_ws())

    assert await cm.get_active_session(1) == 12


async def test_get_active_session_after_reconnect_reflects_recency() -> None:
    cm = ConnectionManager()
    await cm.connect(1, 10, _fake_ws())
    await cm.connect(1, 11, _fake_ws())
    # Reconnect on session 10 — it should now be the most recent.
    await cm.connect(1, 10, _fake_ws())

    assert await cm.get_active_session(1) == 10


async def test_get_active_session_none_when_user_unknown() -> None:
    cm = ConnectionManager()
    assert await cm.get_active_session(user_id=42) is None


async def test_is_user_connected() -> None:
    cm = ConnectionManager()
    assert await cm.is_user_connected(1) is False
    await cm.connect(1, 10, _fake_ws())
    assert await cm.is_user_connected(1) is True
    await cm.disconnect(1, 10)
    assert await cm.is_user_connected(1) is False


# ---------------------------------------------------------------------------
# Lock + singleton
# ---------------------------------------------------------------------------


async def test_concurrent_connects_serialize_under_lock() -> None:
    """Hammer connect with concurrent coroutines and verify final state is consistent.

    If the lock weren't held during the displacement read-modify-write, the
    final dict could miss the displacement of a stale socket.
    """
    cm = ConnectionManager()

    async def connect_one(i: int) -> None:
        await cm.connect(1, 10, _fake_ws())

    await asyncio.gather(*(connect_one(i) for i in range(50)))

    # Exactly one socket should be registered at (1, 10).
    assert list(cm.active_connections[1].keys()) == [10]


def test_module_level_singleton_exists() -> None:
    assert isinstance(manager, ConnectionManager)


@pytest.fixture(autouse=True)
def _reset_global_manager() -> Any:
    """Make sure the module-level singleton stays empty across tests."""
    manager.active_connections.clear()
    yield
    manager.active_connections.clear()
