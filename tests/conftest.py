from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.deps import get_db
from app.db.base import Base
from app.main import app
from app.services import k8s_client as k8s_client_module


@pytest.fixture
def db_engine() -> Iterator[Engine]:
    """Per-test in-memory SQLite engine.

    Uses StaticPool so every connection within the engine reuses the same
    in-memory database. Creating a fresh engine per test gives each test a
    pristine schema with no rows — equivalent to (and simpler than) the
    rollback-per-test pattern called out in spec §UC.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session_factory(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autocommit=False, autoflush=False)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Direct SQLAlchemy session bound to the same engine the TestClient uses.

    Tests can use this to seed rows that the API endpoints will read back.
    """
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    def override_get_db() -> Iterator[Session]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    monkeypatch.setattr(
        k8s_client_module.KubernetesService,
        "get_instance",
        classmethod(lambda cls: MagicMock()),
    )

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    """Register a fresh user and return a Bearer-token Authorization header."""
    resp = client.post(
        "/api/v1/auth/register",
        json={"email": "test@example.com", "password": "test-password"},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}
