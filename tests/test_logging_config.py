"""Tests for app/core/logging_config.py (issue #9, AC #3)."""

import logging

from app.core.logging_config import (
    SecretRedactingFilter,
    _LOG_FORMAT,
    configure_logging,
)


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_filter_redacts_secret_value_in_fstring() -> None:
    f = SecretRedactingFilter()
    f._secrets = ["super-secret-jwt-key"]

    rec = _record("startup: SECRET_KEY=super-secret-jwt-key, ok")
    assert f.filter(rec) is True
    assert "super-secret-jwt-key" not in rec.getMessage()
    assert "[REDACTED]" in rec.getMessage()


def test_filter_redacts_value_passed_as_arg() -> None:
    f = SecretRedactingFilter()
    f._secrets = ["AIzaSyABC123"]

    rec = _record("calling gemini with key=%s", "AIzaSyABC123")
    f.filter(rec)
    assert "AIzaSyABC123" not in rec.getMessage()
    assert "[REDACTED]" in rec.getMessage()


def test_filter_redacts_database_url_password() -> None:
    f = SecretRedactingFilter()
    f._secrets = ["postgresql://user:pw@host:5432/db"]

    rec = _record("db error: postgresql://user:pw@host:5432/db unreachable")
    f.filter(rec)
    assert "user:pw" not in rec.getMessage()
    assert "[REDACTED]" in rec.getMessage()


def test_filter_passthrough_when_no_secret_present() -> None:
    f = SecretRedactingFilter()
    f._secrets = ["hunter2"]

    rec = _record("nothing sensitive here")
    f.filter(rec)
    assert rec.getMessage() == "nothing sensitive here"


def test_filter_handles_empty_secret_list() -> None:
    f = SecretRedactingFilter()
    f._secrets = []
    rec = _record("plain message")
    assert f.filter(rec) is True
    assert rec.getMessage() == "plain message"


def test_configure_logging_attaches_handler_and_filter() -> None:
    configure_logging("INFO")
    root = logging.getLogger()
    assert root.level == logging.INFO
    assert root.handlers, "root must have at least one handler"

    handler = root.handlers[0]
    assert any(isinstance(f, SecretRedactingFilter) for f in handler.filters)


def test_configure_logging_uses_structured_format() -> None:
    configure_logging("INFO")
    handler = logging.getLogger().handlers[0]
    assert handler.formatter is not None
    assert handler.formatter._fmt == _LOG_FORMAT


def test_configure_logging_lowers_uvicorn_access_to_warning() -> None:
    configure_logging("INFO")
    assert logging.getLogger("uvicorn.access").level == logging.WARNING


def test_configure_logging_is_idempotent() -> None:
    configure_logging("INFO")
    configure_logging("INFO")
    root = logging.getLogger()
    # Second call must not have added a duplicate handler.
    assert len(root.handlers) == 1
