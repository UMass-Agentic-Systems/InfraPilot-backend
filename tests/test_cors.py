"""Tests for the CORS middleware wiring in app/main.py (issue #9, AC #2)."""

from fastapi.testclient import TestClient

from app.main import app

_client = TestClient(app)


def test_preflight_from_vite_origin_succeeds() -> None:
    """A browser preflight from Vite's dev origin (5173) must be accepted."""
    resp = _client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "POST" in resp.headers["access-control-allow-methods"]


def test_preflight_from_cra_origin_succeeds() -> None:
    """CRA's default dev origin (3000) is also allowed."""
    resp = _client.options(
        "/api/v1/auth/register",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_simple_request_carries_cors_header() -> None:
    """A simple GET with an allowed Origin should echo the origin header back."""
    resp = _client.get("/health", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_preflight_from_disallowed_origin_is_rejected() -> None:
    """Origins outside the allowlist must NOT receive an Access-Control-Allow-Origin header."""
    resp = _client.options(
        "/health",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.headers.get("access-control-allow-origin") != "http://evil.example.com"
