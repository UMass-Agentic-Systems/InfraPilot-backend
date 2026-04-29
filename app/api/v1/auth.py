import logging
import re

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.api.schemas.auth import LoginRequest, RegisterRequest, TokenResponse
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.services.k8s_client import KubernetesService

logger = logging.getLogger(__name__)
router = APIRouter()

_NS_INVALID = re.compile(r"[^a-z0-9-]")


def _derive_namespace(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    sanitized = _NS_INVALID.sub("-", local)[:58]
    return f"user-{sanitized}"


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> TokenResponse:
    if db.query(User).filter(User.email == payload.email).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        namespace=_derive_namespace(payload.email),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    try:
        KubernetesService.get_instance().ensure_namespace(user.namespace)
    except Exception as exc:
        logger.warning("ensure_namespace(%s) failed: %s", user.namespace, exc)

    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.query(User).filter(User.email == payload.email).first()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    return TokenResponse(access_token=create_access_token(str(user.id)))
