from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.engine import get_db
from app.models import User


password_hasher = PasswordHasher()


# Extracts "Bearer <token>" from the Authorization header.
# JWT validation happens separately in get_current_user().
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login",
)


def hash_password(password: str) -> str:
    """Hash a password using Argon2."""
    return password_hasher.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a password against its Argon2 hash."""
    try:
        return password_hasher.verify(hashed_password, password)
    except (
        VerifyMismatchError,
        VerificationError,
        InvalidHashError,
    ):
        return False


def create_access_token(user_id: UUID) -> str:
    """Create a JWT whose expiration is ACCESS_TOKEN_EXPIRE_MINUTES ahead."""

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )

    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": expires_at,
    }

    token = jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )

    return token


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Validate the bearer JWT and return its database user."""

    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    expired_token_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Access token has expired. Please log in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={
                "require": ["sub", "iat", "exp"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
            },
        )

        subject = payload.get("sub")
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")

        if not isinstance(subject, str):
            raise credentials_error

        # Reject malformed claim types rather than relying on coercion.
        if (
            isinstance(issued_at, bool)
            or not isinstance(issued_at, (int, float))
            or isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
        ):
            raise credentials_error

        # A token with no positive lifetime is invalid.
        if expires_at <= issued_at:
            raise credentials_error

        user_id = UUID(subject)

    except jwt.ExpiredSignatureError:
        raise expired_token_error

    except (InvalidTokenError, ValueError, TypeError):
        raise credentials_error

    result = await db.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_error

    return user
