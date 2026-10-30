import os
import unittest
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://user:pass@localhost/clipfetch_test",
)
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "unit-test-only-placeholder-secret",
)
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
os.environ.setdefault("INVITE_CODE", "unit-test-only-placeholder")

from fastapi.security import OAuth2PasswordRequestForm

from app.models import User
from app.routers import auth
from main import app


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
