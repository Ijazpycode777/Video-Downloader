import os
import base64
import json
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import jwt

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://user:pass@localhost/clipfetch_test",
)
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "unit-test-only-placeholder-secret-with-at-least-32-bytes",
)
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
os.environ.setdefault("INVITE_CODE", "unit-test-only-placeholder-invite")
os.environ.setdefault(
    "COOKIE_ENCRYPTION_KEY",
    base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode(),
)

from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from app.config import Settings, settings
from app.models import User
from app.routers import auth
from app.security import create_access_token
from main import app, handle_database_error


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_operational_error_returns_specific_safe_message(
        self,
    ):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": "/downloads/history",
                "headers": [],
                "query_string": b"",
            }
        )
        error = OperationalError(
            "SELECT secret_data",
            {},
            RuntimeError("private connection detail"),
        )

        response = await handle_database_error(request, error)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            json.loads(bytes(response.body).decode("utf-8"))["detail"],
            (
                "The database is temporarily unavailable. Check the "
                "database connection and retry."
            ),
        )
        self.assertNotIn(b"secret_data", response.body)
        self.assertNotIn(b"private connection detail", response.body)

    def test_authentication_configuration_is_fixed_to_hs256_and_30_minutes(
        self,
    ):
        with self.assertRaises(ValidationError):
            Settings(
                DATABASE_URL=(
                    "postgresql+asyncpg://user:password@localhost/clipfetch"
                ),
                JWT_SECRET_KEY="x" * 32,
                INVITE_CODE="test-invite",
                JWT_ALGORITHM="HS512",
            )
        with self.assertRaises(ValidationError):
            Settings(
                DATABASE_URL=(
                    "postgresql+asyncpg://user:password@localhost/clipfetch"
                ),
                JWT_SECRET_KEY="x" * 32,
                INVITE_CODE="test-invite",
                ACCESS_TOKEN_EXPIRE_MINUTES=60,
            )

    def test_jwt_uses_hs256_and_expires_after_thirty_minutes(self):
        token = create_access_token(uuid.uuid4())
        claims = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=["HS256"],
        )

        self.assertEqual(jwt.get_unverified_header(token)["alg"], "HS256")
        self.assertEqual(claims["exp"] - claims["iat"], 30 * 60)

    async def test_authentication_accepts_email_or_username(self):
        user = User(
            id=uuid.uuid4(),
            username="test-user",
            email="test@example.com",
            hashed_password="test-hash",
        )
        result = Mock()
        result.scalar_one_or_none.return_value = user

        for identifier in (user.email, user.username):
            with self.subTest(identifier=identifier):
                db = AsyncMock()
                db.execute.return_value = result

                with patch.object(
                    auth,
                    "verify_password",
                    return_value=True,
                ) as verify:
                    authenticated_user = await auth.authenticate_user(
                        identifier,
                        "correct-horse",
                        db,
                    )

                self.assertIs(authenticated_user, user)
                statement = str(db.execute.await_args.args[0])
                self.assertIn("users.email", statement)
                self.assertIn("users.username", statement)
                verify.assert_called_once_with(
                    "correct-horse",
                    user.hashed_password,
                )

    async def test_oauth2_form_issues_token_using_email_as_username(self):
        user = User(
            id=uuid.uuid4(),
            username="test-user",
            email="test@example.invalid",
            hashed_password="test-hash",
        )
        form_data = OAuth2PasswordRequestForm(
            username=user.email,
            password="correct-horse",
        )
        db = AsyncMock()

        with (
            patch.object(
                auth,
                "authenticate_user",
                new=AsyncMock(return_value=user),
            ) as authenticate,
            patch.object(
                auth,
                "create_access_token",
                return_value="test-token",
            ),
        ):
            result = await auth.issue_token(form_data, db)

        authenticate.assert_awaited_once_with(
            user.email,
            "correct-horse",
            db,
        )
        self.assertEqual(
            result,
            {
                "access_token": "test-token",
                "token_type": "bearer",
            },
        )

    def test_oauth2_scheme_points_to_form_token_endpoint(self):
        openapi = app.openapi()
        security_scheme = openapi["components"]["securitySchemes"][
            "OAuth2PasswordBearer"
        ]

        self.assertEqual(
            security_scheme["flows"]["password"]["tokenUrl"],
            "/auth/token",
        )
        self.assertIn(
            "application/x-www-form-urlencoded",
            openapi["paths"]["/auth/token"]["post"]["requestBody"][
                "content"
            ],
        )
        self.assertIn(
            {"OAuth2PasswordBearer": []},
            openapi["paths"]["/downloads/"]["post"]["security"],
        )


if __name__ == "__main__":
    unittest.main()
