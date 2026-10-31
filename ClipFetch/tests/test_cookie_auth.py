import asyncio
import base64
from io import BytesIO
import os
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://clipfetch:clipfetch@localhost/clipfetch_test",
)
os.environ.setdefault(
    "JWT_SECRET_KEY",
    "unit-test-only-placeholder-secret-with-at-least-32-bytes",
)
os.environ.setdefault("INVITE_CODE", "unit-test-only-placeholder-invite")
os.environ.setdefault(
    "COOKIE_ENCRYPTION_KEY",
    base64.urlsafe_b64encode(b"0123456789abcdef0123456789abcdef").decode(),
)

from fastapi import HTTPException, UploadFile

from app.models import User, UserProviderCookie
from app.routers import downloads
from app.routers.auth import (
    configure_provider_cookies,
    provider_cookie_status,
    remove_provider_cookies,
)
from app.services.cookies import (
    Cookie,
    CookieConfigurationError,
    active_auth_cookies,
    decrypt_cookies,
    encrypt_cookies,
    is_expired,
    parse_netscape_cookies,
    serialize_netscape_cookies,
)
from app.services.video import (
    RestrictedMediaError,
    ProviderAuthenticationRequiredError,
    _raise_source_error,
    MediaSourceError,
)
from yt_dlp.utils import DownloadError


def youtube_cookie(
    value: str = "opaque-user-token",
    *,
    expires: int = 0,
    name: str = "SID",
) -> Cookie:
    return Cookie(
        domain=".youtube.com",
        include_subdomains=True,
        path="/",
        secure=True,
        expires=expires,
        name=name,
        value=value,
    )


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class CookieSession:
    def __init__(self, cookies_by_user=None):
        self.cookies_by_user = cookies_by_user or {}
        self.statement = None
        self.added = []
        self.commits = 0
        self.deleted = []

    async def execute(self, statement):
        self.statement = statement
        params = statement.compile().params
        user_id = next(
            value for value in params.values() if isinstance(value, uuid.UUID)
        )
        return Result(self.cookies_by_user.get(user_id))

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None

    async def delete(self, value):
        self.deleted.append(value)


def user() -> User:
    return User(
        id=uuid.uuid4(),
        username="cookie-user",
        email="cookie@example.invalid",
        hashed_password="not-a-real-password-hash",
    )


def credential(owner: User, cookies: list[Cookie]) -> UserProviderCookie:
    persistent_expirations = [
        item.expires
        for item in cookies
        if item.is_auth_cookie and item.expires > 0
    ]
    from datetime import datetime, timezone

    return UserProviderCookie(
        user_id=owner.id,
        provider="youtube",
        encrypted_cookies=encrypt_cookies(cookies),
        expires_at=(
            datetime.fromtimestamp(min(persistent_expirations), timezone.utc)
            if persistent_expirations
            else None
        ),
        requires_reauthentication=False,
    )


class CookieStorageTests(unittest.TestCase):
    def test_cookie_expiration_is_preserved_and_expired_auth_is_invalid(self):
        expired_at = int(time.time()) - 60
        imported = parse_netscape_cookies(
            (
                "# Netscape HTTP Cookie File\n"
                f".youtube.com\tTRUE\t/\tTRUE\t{expired_at}\tSID\told-token\n"
            ).encode()
        )
        restored = decrypt_cookies(encrypt_cookies(imported))

        self.assertEqual(restored[0].expires, expired_at)
        self.assertTrue(is_expired(restored))
        self.assertEqual(active_auth_cookies(restored), [])

    def test_session_cookie_remains_session_cookie_without_fabricated_expiry(
        self,
    ):
        from app.services.cookies import next_auth_expiration

        imported = parse_netscape_cookies(
            b".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsession-token\n"
        )
        restored = decrypt_cookies(encrypt_cookies(imported))

        self.assertEqual(restored[0].expires, 0)
        self.assertFalse(is_expired(restored))
        self.assertIsNone(next_auth_expiration(restored))

    def test_cookie_import_rejects_non_youtube_domains(self):
        with self.assertRaises(CookieConfigurationError):
            parse_netscape_cookies(
                b".google.com\tTRUE\t/\tTRUE\t0\tSID\tother-site-token\n"
            )

    def test_serialized_cookie_file_excludes_expired_cookies(self):
        now = int(time.time())
        imported = [
            youtube_cookie("active", expires=now + 1000),
            youtube_cookie("old", expires=now - 100, name="APISID"),
            youtube_cookie("session", expires=0, name="SAPISID"),
        ]
        from app.services.cookies import current_cookies

        output = serialize_netscape_cookies(current_cookies(imported, now=now))
        self.assertIn(b"SID\tactive", output)
        self.assertIn(b"SAPISID\tsession", output)
        self.assertNotIn(b"APISID\told", output)


class PerUserCookieAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_uses_a_single_conditional_user_cookie_retry(self):
        owner = user()
        stored = credential(owner, [youtube_cookie("only-this-user")])
        db = CookieSession({owner.id: stored})
        request = downloads.DownloadRequest.model_validate(
            {"url": "https://youtube.com/watch?v=example"}
        )
        file_response = object()
        download_calls = []

        async def downloader(**kwargs):
            download_calls.append(kwargs)
            if len(download_calls) == 1:
                raise ProviderAuthenticationRequiredError()
            return {"filename": "result.mp4", "filepath": "temporary.mp4"}

        with (
            patch.object(downloads, "download_media", side_effect=downloader),
            patch.object(downloads, "_file_response", return_value=file_response),
        ):
            response = await downloads.create_download(
                request,
                owner,
                db,  # type: ignore[arg-type]
            )

        self.assertIs(response, file_response)
        self.assertEqual(len(download_calls), 2)
        self.assertNotIn("cookie_data", download_calls[0])
        self.assertIn(b"only-this-user", download_calls[1]["cookie_data"])
        self.assertEqual(db.commits, 1)

    async def test_cookie_import_encrypts_before_persisting(self):
        owner = user()
        db = CookieSession()
        upload = UploadFile(
            filename="youtube-cookies.txt",
            file=BytesIO(
                b".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tprivate-cookie-value\n"
            ),
        )

        result = await configure_provider_cookies(
            "youtube",
            upload,
            owner,
            db,  # type: ignore[arg-type]
        )

        stored = db.added[0]
        self.assertEqual(result["status"], "configured")
        self.assertNotIn(b"private-cookie-value", stored.encrypted_cookies)
        self.assertEqual(
            decrypt_cookies(stored.encrypted_cookies)[0].value,
            "private-cookie-value",
        )
        self.assertEqual(stored.user_id, owner.id)
        self.assertEqual(stored.provider, "youtube")
        self.assertEqual(db.commits, 1)

    async def test_cookie_import_rejects_an_unrelated_provider(self):
        with self.assertRaises(HTTPException) as raised:
            await configure_provider_cookies(
                "google",
                None,  # type: ignore[arg-type]
                user(),
                CookieSession(),  # type: ignore[arg-type]
            )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_remove_cookie_deletes_only_current_users_record(self):
        owner = user()
        other = user()
        own_cookie = credential(owner, [youtube_cookie()])
        db = CookieSession({owner.id: own_cookie, other.id: credential(
            other, [youtube_cookie("other-user-cookie")]
        )})

        response = await remove_provider_cookies(
            "youtube",
            owner,
            db,  # type: ignore[arg-type]
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(db.deleted, [own_cookie])
        self.assertEqual(db.commits, 1)
        self.assertFalse(any(
            record.user_id == other.id for record in db.deleted
        ))

    async def test_cookie_status_uses_only_the_authenticated_users_record(self):
        owner = user()
        other = user()
        own_cookie = credential(owner, [youtube_cookie("owner-secret")])
        db = CookieSession({owner.id: own_cookie})

        response = await provider_cookie_status(
            "youtube",
            owner,
            db,  # type: ignore[arg-type]
        )

        self.assertEqual(response.status, "configured")
        self.assertIn("user_provider_cookies.user_id", str(db.statement))
        self.assertNotIn("owner-secret", response.model_dump_json())
        other_db = CookieSession({owner.id: own_cookie})
        other_response = await provider_cookie_status(
            "youtube",
            other,
            other_db,  # type: ignore[arg-type]
        )
        self.assertEqual(other_response.status, "not_configured")

    async def test_authentication_challenge_retries_once_with_user_cookies(
        self,
    ):
        owner = user()
        expiration = int(time.time()) + 600
        imported = [
            youtube_cookie("user-one-secret", expires=expiration),
            youtube_cookie("expired-secret", expires=expiration - 1000, name="APISID"),
        ]
        stored = credential(owner, imported)
        db = CookieSession({owner.id: stored})
        request = downloads.DownloadRequest.model_validate(
            {"url": "https://www.youtube.com/watch?v=example"}
        )
        calls = []

        async def downloader(**kwargs):
            self.assertEqual(kwargs["url"], str(request.url))
            calls.append(kwargs)
            return {"filename": "video.mp4", "filepath": "unused"}

        with patch.object(downloads, "download_media", side_effect=downloader):
            await downloads._retry_with_user_cookies(
                data=request,
                current_user=owner,
                db=db,  # type: ignore[arg-type]
            )

        self.assertEqual(len(calls), 1)
        self.assertIn(b"SID\tuser-one-secret", calls[0]["cookie_data"])
        self.assertNotIn(b"expired-secret", calls[0]["cookie_data"])
        self.assertEqual(calls[0]["url"], str(request.url))

    async def test_expired_cookies_are_never_passed_to_downloader(self):
        owner = user()
        stored = credential(
            owner,
            [youtube_cookie("expired-secret", expires=int(time.time()) - 5)],
        )
        db = CookieSession({owner.id: stored})
        request = downloads.DownloadRequest.model_validate(
            {"url": "https://youtube.com/watch?v=example"}
        )
        downloader = AsyncMock()

        with patch.object(downloads, "download_media", downloader):
            with self.assertRaises(HTTPException) as raised:
                await downloads._retry_with_user_cookies(
                    data=request,
                    current_user=owner,
                    db=db,  # type: ignore[arg-type]
                )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertIn("expired", raised.exception.detail)
        downloader.assert_not_awaited()

    async def test_rejected_cookie_marks_only_own_record_for_reauthentication(
        self,
    ):
        owner = user()
        other = user()
        own_cookie = credential(owner, [youtube_cookie("owner-secret")])
        other_cookie = credential(other, [youtube_cookie("other-secret")])
        db = CookieSession({owner.id: own_cookie, other.id: other_cookie})
        request = downloads.DownloadRequest.model_validate(
            {"url": "https://youtube.com/watch?v=example"}
        )
        calls = []

        async def downloader(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ProviderAuthenticationRequiredError()
            raise downloads.ProviderAuthenticationRejectedError()

        with patch.object(downloads, "download_media", side_effect=downloader):
            with self.assertRaises(HTTPException) as raised:
                await downloads.create_download(
                    request,
                    owner,
                    db,  # type: ignore[arg-type]
                )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertNotIn("cookie_data", calls[0])
        self.assertIn(b"owner-secret", calls[1]["cookie_data"])
        self.assertTrue(own_cookie.requires_reauthentication)
        self.assertFalse(other_cookie.requires_reauthentication)
        self.assertEqual(db.commits, 1)

    async def test_two_users_retry_concurrently_with_isolated_credentials(self):
        first = user()
        second = user()
        first_cookie = credential(first, [youtube_cookie("first-secret")])
        second_cookie = credential(second, [youtube_cookie("second-secret")])
        captured = {}
        request = downloads.DownloadRequest.model_validate(
            {"url": "https://youtube.com/watch?v=example"}
        )

        async def downloader(**kwargs):
            captured[kwargs["cookie_data"]] = kwargs["cookie_data"]
            return {"filename": "video.mp4", "filepath": "unused"}

        with patch.object(downloads, "download_media", side_effect=downloader):
            await asyncio.gather(
                downloads._retry_with_user_cookies(
                    data=request,
                    current_user=first,
                    db=CookieSession({first.id: first_cookie}),  # type: ignore[arg-type]
                ),
                downloads._retry_with_user_cookies(
                    data=request,
                    current_user=second,
                    db=CookieSession({second.id: second_cookie}),  # type: ignore[arg-type]
                ),
            )

        payloads = list(captured)
        self.assertEqual(len(payloads), 2)
        self.assertTrue(any(b"first-secret" in item for item in payloads))
        self.assertTrue(any(b"second-secret" in item for item in payloads))
        self.assertFalse(any(
            b"first-secret" in item and b"second-secret" in item
            for item in payloads
        ))

    async def test_unrelated_source_error_does_not_trigger_cookie_retry(self):
        challenge = DownloadError("HTTP Error 503: service unavailable")

        with self.assertRaises(MediaSourceError):
            _raise_source_error(
                challenge,
                url="https://youtube.com/watch?v=example",
                has_cookies=False,
            )

        with self.assertRaises(MediaSourceError):
            _raise_source_error(
                DownloadError("Sign in to confirm you're not a bot"),
                url="https://example.com/video",
                has_cookies=False,
            )

    async def test_age_or_paid_restrictions_are_never_classified_for_retry(self):
        for message in (
            "Sign in to confirm your age",
            "This is a members-only video",
            "This video is not available in your country",
            "DRM protected stream",
        ):
            with self.subTest(message=message):
                with self.assertRaises(RestrictedMediaError):
                    _raise_source_error(
                        DownloadError(message),
                        url="https://youtube.com/watch?v=example",
                        has_cookies=False,
                    )


if __name__ == "__main__":
    unittest.main()
