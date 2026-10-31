import secrets
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import Response
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.engine import get_db
from app.models import User, UserProviderCookie
from app.security import (
    create_access_token,
    hash_password,
    verify_password,
    get_current_user,
)
from app.services.cookies import (
    MAX_COOKIE_FILE_BYTES,
    CookieConfigurationError,
    CookieDecryptionError,
    CookieEncryptionUnavailable,
    YOUTUBE_PROVIDER,
    active_auth_cookies,
    decrypt_cookies,
    encrypt_cookies,
    is_expired,
    next_auth_expiration,
    parse_netscape_cookies,
)

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"],
)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    invite_code: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


Provider = Literal["youtube"]


class CookieStatusResponse(BaseModel):
    provider: Provider
    status: Literal[
        "not_configured",
        "configured",
        "expired",
        "reauthentication_required",
    ]
    expires_at: datetime | None
    session_cookies: int


def _validate_provider(provider: str) -> Provider:
    if provider != YOUTUBE_PROVIDER:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "Cookie authentication is currently supported only for "
                "YouTube."
            ),
        )
    return "youtube"


async def authenticate_user(
    identifier: str,
    password: str,
    db: AsyncSession,
) -> User:
    result = await db.execute(
        select(User).where(
            (User.email == identifier)
            | (User.username == identifier)
        )
    )
    user = result.scalar_one_or_none()

    if user is None or not verify_password(
        password, user.hashed_password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username/email or password",
        )

    return user


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    data: RegisterRequest,
    db: AsyncSession = Depends(get_db),
):
    if not secrets.compare_digest(data.invite_code, settings.INVITE_CODE):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid invite code",
        )

    result = await db.execute(
        select(User).where(
            (User.username == data.username)
            | (User.email == str(data.email))
        )
    )
    existing_user = result.scalar_one_or_none()

    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already registered",
        )

    user = User(
        username=data.username,
        email=str(data.email),
        hashed_password=hash_password(data.password),
    )

    db.add(user)

    try:
        await db.commit()
        await db.refresh(user)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already registered",
        )

    return {
        "message": "Registration successful",
        "user": {
            "id": str(user.id),
            "username": user.username,
            "email": user.email,
        },
    }


@router.post("/login")
async def login(
    data: LoginRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(str(data.email), data.password, db)
    access_token = create_access_token(user.id)

    return {
        "access_token": access_token,
        "token_type": "bearer",
    }


@router.post("/token")
async def issue_token(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with the account username or email."""
    user = await authenticate_user(
        form_data.username,
        form_data.password,
        db,
    )

    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
    }


@router.get("/me")
async def get_profile(current_user: User = Depends(get_current_user)):
    return {
        "id": str(current_user.id),
        "username": current_user.username,
        "email": current_user.email
    }


@router.post(
    "/me/cookies/{provider}",
    status_code=status.HTTP_201_CREATED,
)
async def configure_provider_cookies(
    provider: str,
    file: Annotated[UploadFile, File(description=(
        "User-exported Netscape-format cookie file. YouTube-domain cookies "
        "only; cookie values are encrypted before database storage."
    ))],
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    supported_provider = _validate_provider(provider)
    try:
        content = await file.read(MAX_COOKIE_FILE_BYTES + 1)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The uploaded cookie file could not be read.",
        ) from exc
    finally:
        await file.close()

    try:
        cookies = parse_netscape_cookies(content)
        encrypted = encrypt_cookies(cookies)
    except CookieConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except CookieEncryptionUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    result = await db.execute(
        select(UserProviderCookie).where(
            UserProviderCookie.user_id == current_user.id,
            UserProviderCookie.provider == supported_provider,
        )
    )
    stored = result.scalar_one_or_none()
    expiration = next_auth_expiration(cookies)
    if stored is None:
        stored = UserProviderCookie(
            user_id=current_user.id,
            provider=supported_provider,
            encrypted_cookies=encrypted,
            expires_at=expiration,
            requires_reauthentication=False,
        )
        db.add(stored)
    else:
        stored.encrypted_cookies = encrypted
        stored.expires_at = expiration
        stored.requires_reauthentication = False
        stored.updated_at = datetime.now(timezone.utc)

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "YouTube credentials were changed concurrently. Retry the "
                "import; no credential content was returned."
            ),
        ) from exc

    return {
        "message": "YouTube cookie credentials were encrypted and saved.",
        "provider": supported_provider,
        "status": "configured",
        "expires_at": expiration,
    }


@router.get(
    "/me/cookies/{provider}/status",
    response_model=CookieStatusResponse,
)
async def provider_cookie_status(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CookieStatusResponse:
    supported_provider = _validate_provider(provider)
    result = await db.execute(
        select(UserProviderCookie).where(
            UserProviderCookie.user_id == current_user.id,
            UserProviderCookie.provider == supported_provider,
        )
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        return CookieStatusResponse(
            provider=supported_provider,
            status="not_configured",
            expires_at=None,
            session_cookies=0,
        )

    try:
        cookies = decrypt_cookies(stored.encrypted_cookies)
    except (CookieEncryptionUnavailable, CookieDecryptionError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    if is_expired(cookies):
        state = "expired"
    elif stored.requires_reauthentication:
        state = "reauthentication_required"
    else:
        state = "configured"

    session_cookies = sum(
        1 for cookie in active_auth_cookies(cookies) if cookie.is_session_cookie
    )
    return CookieStatusResponse(
        provider=supported_provider,
        status=state,
        expires_at=next_auth_expiration(cookies),
        session_cookies=session_cookies,
    )


@router.delete(
    "/me/cookies/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_provider_cookies(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    supported_provider = _validate_provider(provider)
    result = await db.execute(
        select(UserProviderCookie).where(
            UserProviderCookie.user_id == current_user.id,
            UserProviderCookie.provider == supported_provider,
        )
    )
    stored = result.scalar_one_or_none()
    if stored is not None:
        await db.delete(stored)
        await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)