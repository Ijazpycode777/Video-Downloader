from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.engine import get_db
from app.models import User


password_hasher = PasswordHasher()

# Allow only a small difference between system clocks.
JWT_CLOCK_SKEW_SECONDS = 5


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
    """Create a signed JWT with a valid issue time and expiration."""
    now = int(datetime.now(timezone.utc).timestamp())

    expires_at = now + (
        settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "iat": now,
        "exp": expires_at,
    }

    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(
        bearer_scheme
    ),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Validate the JWT and return the authenticated user."""

    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        token = credentials.credentials

        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            leeway=JWT_CLOCK_SKEW_SECONDS,
            options={
                "require": ["sub", "iat", "exp"],
            },
        )

        subject = payload.get("sub")

        if not isinstance(subject, str):
            raise credentials_error

        user_id = UUID(subject)

    except (jwt.InvalidTokenError, ValueError):
        raise credentials_error

    result = await db.execute(
        select(User).where(User.id == user_id)
    )

    user = result.scalar_one_or_none()

    if user is None:
        raise credentials_error

    return user
