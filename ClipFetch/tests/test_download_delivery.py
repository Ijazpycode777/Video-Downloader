import os
import base64
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

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

from fastapi import HTTPException

from app.models import Base, DownloadRecord, User
from app.routers import downloads
from app.services import storage
from app.services import video
from app.services.video import MediaSourceError, VideoDownloadError
from yt_dlp.utils import DownloadError, PostProcessingError


class ScalarResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class QueryResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return ScalarResult(self._values)


class FakeSession:
    def __init__(self, history_items=None) -> None:
        self.records: list[DownloadRecord] = []
        self.commits = 0
        self.history_items = history_items or []
        self.statement = None

    def add(self, record: DownloadRecord) -> None:
        self.records.append(record)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.records.clear()

    async def execute(self, statement):
        self.statement = statement
        return QueryResult(self.history_items)


def make_user() -> User:
    return User(
        id=uuid.uuid4(),
        username="test-user",
        email="test@example.invalid",
        hashed_password="not-a-real-password-hash",
    )


def make_request(url: str) -> downloads.DownloadRequest:
    return downloads.DownloadRequest.model_validate({"url": url})


class DownloadDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_database_schema_has_filename_history_not_jobs(self):
        self.assertEqual(
            set(Base.metadata.tables),
            {"users", "download_history", "user_provider_cookies"},
        )
        self.assertEqual(
            set(Base.metadata.tables["download_history"].columns.keys()),
            {"id", "user_id", "filename", "downloaded_at"},
        )

    async def asyncSetUp(self) -> None:
        self.temp_directory = tempfile.TemporaryDirectory()
        self.download_root = Path(self.temp_directory.name)
        self.media_root = self.download_root / ".clipfetch-media"
        self.media_root.mkdir()

        self.original_download_dir = storage.DOWNLOAD_DIR
        self.original_media_dir = storage.MEDIA_DIR
        self.original_lock_path = storage.LOCK_PATH
        self.original_video_media_dir = video.MEDIA_DIR
        storage.DOWNLOAD_DIR = self.download_root
        storage.MEDIA_DIR = self.media_root
        storage.LOCK_PATH = self.media_root / ".storage.lock"
        video.MEDIA_DIR = self.media_root

    async def asyncTearDown(self) -> None:
        storage.DOWNLOAD_DIR = self.original_download_dir
        storage.MEDIA_DIR = self.original_media_dir
        storage.LOCK_PATH = self.original_lock_path
        video.MEDIA_DIR = self.original_video_media_dir
        self.temp_directory.cleanup()

    def make_media_file(self, contents: bytes = b"media") -> Path:
        path = self.media_root / f"{uuid.uuid4().hex}.mp4"
        path.write_bytes(contents)
        return path

    async def consume(self, response) -> bytes:
        body = bytearray()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.body":
                body.extend(message.get("body", b""))

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/downloads/",
            "raw_path": b"/downloads/",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
            "root_path": "",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }
        await response(scope, receive, send)
        return bytes(body)

    async def test_post_returns_attachment_and_records_user_filename(self):
        contents = b"downloaded mp4 bytes"
        media_path = self.make_media_file(contents)
        db = FakeSession()
        user = make_user()
        result = {
            "title": "Sample video",
            "filename": "Sample video.mp4",
            "filepath": str(media_path),
            "duration": 12,
            "file_size": len(contents),
        }

        with patch.object(
            downloads,
            "download_media",
            new=AsyncMock(return_value=result),
        ):
            response = await downloads.create_download(
                make_request("https://www.facebook.com/example"),
                user,
                cast(AsyncSession, db),
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.media_type, "video/mp4")
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn(
            "Sample%20video.mp4",
            response.headers["content-disposition"],
        )
        self.assertNotIn("x-download-job-id", response.headers)
        self.assertEqual(db.records, [])
        self.assertEqual(db.commits, 0)
        self.assertEqual(await self.consume(response), contents)
        self.assertEqual(len(db.records), 1)
        self.assertEqual(db.records[0].user_id, user.id)
        self.assertEqual(db.records[0].filename, "Sample video.mp4")
        self.assertEqual(db.commits, 1)
        self.assertFalse(media_path.exists())

    async def test_interrupted_response_is_not_added_to_history(self):
        media_path = self.make_media_file(b"transfer that will be interrupted")
        db = FakeSession()
        user = make_user()
        result = {
            "filename": "Interrupted.mp4",
            "filepath": str(media_path),
        }

        with patch.object(
            downloads,
            "download_media",
            new=AsyncMock(return_value=result),
        ):
            response = await downloads.create_download(
                make_request("https://www.facebook.com/example"),
                user,
                cast(AsyncSession, db),
            )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def disconnecting_send(message):
            if (
                message["type"] == "http.response.body"
                and message.get("body")
            ):
                raise OSError("client disconnected during response")

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/downloads/",
            "raw_path": b"/downloads/",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
            "root_path": "",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }
        with self.assertRaises(OSError):
            await response(scope, receive, disconnecting_send)

        self.assertEqual(db.records, [])
        self.assertEqual(db.commits, 0)
        self.assertTrue(media_path.exists())

    async def test_failed_download_is_not_added_to_history(self):
        db = FakeSession()

        with patch.object(
            downloads,
            "download_media",
            new=AsyncMock(
                side_effect=VideoDownloadError("source unavailable")
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await downloads.create_download(
                    make_request("https://www.instagram.com/example"),
                    make_user(),
                    cast(AsyncSession, db),
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(db.records, [])
        self.assertEqual(db.commits, 0)

    async def test_storage_download_error_returns_its_specific_message(self):
        db = FakeSession()
        user = make_user()

        with patch.object(
            downloads,
            "download_media",
            new=AsyncMock(
                side_effect=video.MediaStorageError(
                    "Storage directory is not writable."
                )
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await downloads.create_download(
                    make_request("https://example.com/video"),
                    user,
                    cast(AsyncSession, db),
                )

        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(
            raised.exception.detail,
            "Storage directory is not writable.",
        )

    async def test_history_returns_only_names_and_timestamps(self):
        user = make_user()
        timestamp = datetime.now(timezone.utc)
        record = DownloadRecord(
            user_id=user.id,
            filename="Previously downloaded.mp4",
            downloaded_at=timestamp,
        )
        db = FakeSession([record])

        history = await downloads.list_download_history(
            user,
            cast(AsyncSession, db),
        )

        self.assertEqual(
            history,
            [
                downloads.DownloadHistoryItem(
                    filename="Previously downloaded.mp4",
                    downloaded_at=timestamp,
                )
            ],
        )
        self.assertIn("download_history.user_id", str(db.statement))
        self.assertNotIn("id", history[0].model_dump())

    async def test_legacy_file_retrieval_route_is_not_registered(self):
        from main import app

        paths = app.openapi()["paths"]
        self.assertIn("/downloads/history", paths)
        self.assertFalse(any(path.endswith("/file") for path in paths))

    async def test_history_failure_after_send_is_logged_without_false_history(
        self,
    ):
        from sqlalchemy.exc import SQLAlchemyError

        class FailingSession(FakeSession):
            async def commit(self) -> None:
                raise SQLAlchemyError("database unavailable")

        media_path = self.make_media_file()
        db = FailingSession()

        with patch.object(
            downloads,
            "download_media",
            new=AsyncMock(
                return_value={
                    "filename": "Sample.mp4",
                    "filepath": str(media_path),
                }
            ),
        ):
            response = await downloads.create_download(
                make_request("https://example.com/video"),
                make_user(),
                cast(AsyncSession, db),
            )

        with self.assertLogs("app.routers.downloads", level="ERROR"):
            self.assertEqual(await self.consume(response), b"media")

        self.assertEqual(db.records, [])
        self.assertEqual(db.commits, 0)
        self.assertFalse(media_path.exists())

    async def test_yt_dlp_failures_return_safe_category_specific_messages(self):
        test_cases = (
            (
                DownloadError(
                    "private token from https://example.com/?token=secret"
                ),
                video.MediaSourceError,
                "yt-dlp could not retrieve this source.",
            ),
            (
                PostProcessingError("ffmpeg output"),
                video.MediaProcessingError,
                "FFmpeg could not process this media.",
            ),
        )

        for error, expected_type, message in test_cases:
            with self.subTest(error=type(error).__name__):
                class FailingDownloader:
                    def __init__(self, options):
                        self.options = options

                    def __enter__(self):
                        return self

                    def __exit__(self, *_: object) -> None:
                        return None

                    def extract_info(self, url: str, *, download: bool):
                        self.url = url
                        self.download_requested = download
                        raise error

                with patch.object(
                    video.yt_dlp,
                    "YoutubeDL",
                    FailingDownloader,
                ):
                    with self.assertRaises(expected_type) as raised:
                        video._download_media(
                            "https://example.com/video",
                            "video",
                            "best",
                        )

                self.assertIn(message, str(raised.exception))
                self.assertEqual(
                    raised.exception.status_code,
                    expected_type.status_code,
                )
                self.assertNotIn("secret", str(raised.exception))

    async def test_failed_processing_removes_partial_files(self):
        class FailingDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def extract_info(self, url: str, *, download: bool):
                self.requested_url = url
                self.download_requested = download
                output_template = Path(self.options["outtmpl"])
                partial_path = output_template.parent / "partial.mp4"
                partial_path.write_bytes(b"partial")
                raise RuntimeError("simulated processing failure")

        with patch.object(video.yt_dlp, "YoutubeDL", FailingDownloader):
            with self.assertLogs("app.services.video", level="ERROR"):
                with self.assertRaises(VideoDownloadError):
                    video._download_media(
                        "https://x.com/example",
                        "video",
                        "best",
                    )

        self.assertEqual(
            list(self.media_root.glob(".clipfetch-working-*")),
            [],
        )

    async def test_temporary_cookie_file_is_removed_after_success(self):
        created_cookie_paths = []
        cookie_data = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"

        class SuccessfulDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def extract_info(self, url: str, *, download: bool):
                cookie_path = Path(self.options["cookiefile"])
                created_cookie_paths.append(cookie_path)
                self_cookie_data = cookie_path.read_bytes()
                if self_cookie_data != cookie_data:
                    raise AssertionError("Unexpected temporary cookie data.")
                Path(cookie_path.parent / "result.mp4").write_bytes(b"media")
                return {"title": "Authenticated video"}

        with patch.object(video.yt_dlp, "YoutubeDL", SuccessfulDownloader):
            result = video._download_media(
                "https://youtube.com/watch?v=example",
                "video",
                "best",
                cookie_data,
            )

        self.assertTrue(Path(result["filepath"]).exists())
        self.assertEqual(len(created_cookie_paths), 1)
        self.assertFalse(created_cookie_paths[0].exists())
        self.assertFalse(created_cookie_paths[0].parent.exists())

    async def test_temporary_cookie_file_is_removed_after_failure(self):
        created_cookie_paths = []
        cookie_data = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"

        class FailingDownloader:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def extract_info(self, url: str, *, download: bool):
                cookie_path = Path(self.options["cookiefile"])
                created_cookie_paths.append(cookie_path)
                self_cookie_data = cookie_path.read_bytes()
                if self_cookie_data != cookie_data:
                    raise AssertionError("Unexpected temporary cookie data.")
                raise DownloadError("network unavailable")

        with patch.object(video.yt_dlp, "YoutubeDL", FailingDownloader):
            with self.assertRaises(MediaSourceError):
                video._download_media(
                    "https://youtube.com/watch?v=example",
                    "video",
                    "best",
                    cookie_data,
                )

        self.assertEqual(len(created_cookie_paths), 1)
        self.assertFalse(created_cookie_paths[0].exists())
        self.assertFalse(created_cookie_paths[0].parent.exists())

    async def test_media_expires_after_thirty_minutes(self):
        media_path = self.make_media_file()
        old_time = time.time() - storage.RETENTION_SECONDS - 1
        os.utime(media_path, (old_time, old_time))
        unrelated_path = self.download_root / "Eclipse.mp4"
        unrelated_path.write_bytes(b"unrelated")

        removed = storage.cleanup_expired_files()

        self.assertEqual(removed, 1)
        self.assertFalse(media_path.exists())
        self.assertTrue(unrelated_path.exists())

    async def test_stale_partial_download_is_cleaned(self):
        working_directory = (
            self.media_root
            / f"{storage.WORKING_DIRECTORY_PREFIX}{uuid.uuid4().hex}"
        )
        working_directory.mkdir()
        (working_directory / "partial.mp4.part").write_bytes(b"partial")
        old_time = time.time() - storage.RETENTION_SECONDS - 1
        os.utime(working_directory, (old_time, old_time))

        self.assertEqual(storage.cleanup_expired_files(), 1)
        self.assertFalse(working_directory.exists())

    async def test_cleanup_does_not_remove_active_transfer(self):
        media_path = self.make_media_file()
        old_time = time.time() - storage.RETENTION_SECONDS - 1
        os.utime(media_path, (old_time, old_time))

        lease = storage.acquire_storage_lease(shared=True)
        if lease is None:
            self.fail("Could not acquire the test transfer lock.")
        with lease:
            self.assertEqual(storage.cleanup_expired_files(), 0)
            self.assertTrue(media_path.exists())

        self.assertEqual(storage.cleanup_expired_files(), 1)
        self.assertFalse(media_path.exists())


if __name__ == "__main__":
    unittest.main()
