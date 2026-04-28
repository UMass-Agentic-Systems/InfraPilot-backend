from datetime import timedelta

import pytest
from jose import ExpiredSignatureError

from app.core.config import settings
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_settings_defaults():
    assert (
        settings.DATABASE_URL
        == "postgresql://postgres:postgres@localhost:5432/infrapilot"
    )
    assert settings.ALGORITHM == "HS256"
    assert settings.ACCESS_TOKEN_EXPIRE_MINUTES == 60


def test_secret_key_not_in_repr():
    r = repr(settings)
    assert "change-me" not in r


def test_google_api_key_not_in_repr():
    r = repr(settings)
    assert (
        settings.GOOGLE_API_KEY.get_secret_value() not in r
        or settings.GOOGLE_API_KEY.get_secret_value() == ""
    )


def test_hash_and_verify():
    hashed = hash_password("correct-password")
    assert verify_password("correct-password", hashed)
    assert not verify_password("wrong-password", hashed)


def test_72_byte_truncation():
    long_password = "a" * 100
    truncated_password = "a" * 72
    hashed = hash_password(long_password)
    assert verify_password(truncated_password, hashed)


def test_create_and_decode_token():
    token = create_access_token("user-123")
    claims = decode_access_token(token)
    assert claims["sub"] == "user-123"


def test_expired_token_raises():
    token = create_access_token("user-123", expires_delta=timedelta(seconds=-1))
    with pytest.raises(ExpiredSignatureError):
        decode_access_token(token)
