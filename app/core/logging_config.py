import logging
from typing import Any

from pydantic import SecretStr

from app.core.config import settings

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
_REDACTED = "[REDACTED]"
_SECRET_FIELDS: tuple[str, ...] = ("SECRET_KEY", "GOOGLE_API_KEY", "DATABASE_URL")


def _collect_secret_values() -> list[str]:
    values: list[str] = []
    for name in _SECRET_FIELDS:
        raw: Any = getattr(settings, name, None)
        if isinstance(raw, SecretStr):
            raw = raw.get_secret_value()
        if raw:
            values.append(str(raw))
    return values


class SecretRedactingFilter(logging.Filter):
    """Replaces configured secret values with `[REDACTED]` in every log record.

    Operates on the fully-formatted message (after `record.getMessage()`) so it
    catches secrets passed via `%`-style args as well as f-strings. After
    redaction the args are cleared so downstream handlers/formatters don't
    re-substitute the originals.
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = _collect_secret_values()

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, _REDACTED)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Idempotent root-logger setup: structured format + secret redaction.

    Replaces any existing root handlers so calling this more than once (e.g.
    in tests) doesn't double-emit. Lowers `uvicorn.access` to WARNING to keep
    development output readable.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    handler.addFilter(SecretRedactingFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
