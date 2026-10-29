import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://test:test@localhost/clipfetch_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "unit-test-only-placeholder")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "30")
os.environ.setdefault("INVITE_CODE", "unit-test-only-placeholder")

from fastapi import HTTPException

from app.models import DownloadJob, User
from app.routers import downloads
from app.services import storage
from app.services import video
from app.services.video import VideoDownloadError


class FakeSession:
    def __init__(self) -> None:
        self.jobs: dict[uuid.UUID, DownloadJob] = {}
        self.commits = 0
        self.get_calls: list[tuple[type[DownloadJob], bool]] = []

    def add(self, job: DownloadJob) -> None:
        if job.id is None:
            job.id = uuid.uuid4()
        self.jobs[job.id] = job

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, job: DownloadJob) -> None:
        self.jobs[job.id] = job

    async def rollback(self) -> None:
        return None

    async def get(
        self,
        model: type[DownloadJob],
        job_id: uuid.UUID,
        *,
        populate_existing: bool = False,
    ) -> DownloadJob | None:
        self.get_calls.append((model, populate_existing))
        return self.jobs.get(job_id)


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
            "method": "GET",
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

    async def test_post_returns_media_attachment_and_cleans_after_delivery(self):
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
        self.assertEqual(
            response.headers["x-download-job-id"],
            str(next(iter(db.jobs))),
        )
        self.assertEqual(next(iter(db.jobs.values())).status, "completed")
        self.assertEqual(await self.consume(response), contents)
        self.assertFalse(media_path.exists())

    async def test_failed_download_is_recorded(self):
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
        job = next(iter(db.jobs.values()))
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error_message, "source unavailable")

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

    async def test_job_ownership_is_enforced(self):
        owner = make_user()
        other_user = make_user()
        job = DownloadJob(
            id=uuid.uuid4(),
            user_id=owner.id,
            video_url="https://x.com/example",
            status="completed",
        )
        db = FakeSession()
        db.jobs[job.id] = job

        with self.assertRaises(HTTPException) as raised:
            await downloads.retrieve_download(
                job.id,
                other_user,
                cast(AsyncSession, db),
            )

        self.assertEqual(raised.exception.status_code, 404)

    async def test_job_owner_can_retrieve_attachment(self):
        contents = b"retrievable media"
        media_path = self.make_media_file(contents)
        owner = make_user()
        job = DownloadJob(
            id=uuid.uuid4(),
            user_id=owner.id,
            video_url="https://x.com/example",
            status="completed",
            filename="Owned video.mp4",
            file_path=str(media_path),
        )
        db = FakeSession()
        db.jobs[job.id] = job

        response = await downloads.retrieve_download(
            job.id,
            owner,
            cast(AsyncSession, db),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(await self.consume(response), contents)
        self.assertFalse(media_path.exists())

    async def test_client_disconnect_retains_file_until_expiry(self):
        media_path = self.make_media_file()
        user = make_user()
        job = DownloadJob(
            id=uuid.uuid4(),
            user_id=user.id,
            video_url="https://x.com/example",
            status="completed",
            filename="Interrupted video.mp4",
            file_path=str(media_path),
        )
        db = FakeSession()
        db.jobs[job.id] = job
        response = await downloads.retrieve_download(
            job.id,
            user,
            cast(AsyncSession, db),
        )

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def disconnecting_send(message):
            if message["type"] == "http.response.body":
                raise OSError("simulated client disconnect")

        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
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

        self.assertTrue(media_path.exists())
        old_time = time.time() - storage.RETENTION_SECONDS - 1
        os.utime(media_path, (old_time, old_time))
        self.assertEqual(storage.cleanup_expired_files(), 1)
        self.assertFalse(media_path.exists())

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
            removed = storage.cleanup_expired_files()
            self.assertEqual(removed, 0)
            self.assertTrue(media_path.exists())

        self.assertEqual(storage.cleanup_expired_files(), 1)
        self.assertFalse(media_path.exists())


if __name__ == "__main__":
    unittest.main()
