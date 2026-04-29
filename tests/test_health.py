"""Smoke tests for /health (UC22, NFR §12.2)."""

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_health_does_not_require_auth(client: TestClient) -> None:
    """Health probe must work without any Authorization header (NFR §12.2)."""
    resp = client.get("/health")
    assert resp.status_code == 200
