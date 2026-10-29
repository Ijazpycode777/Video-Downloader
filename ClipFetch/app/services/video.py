import asyncio
import ipaddress
import os
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, TypedDict, cast
from urllib.parse import urlsplit
import logging
import imageio_ffmpeg
import yt_dlp

from app.services.storage import (
    MEDIA_DIR,
    WORKING_DIRECTORY_PREFIX,
    acquire_storage_lease,
)

logger = logging.getLogger(__name__)
MediaType = Literal["video", "mp3"]

VIDEO_QUALITIES = {"360", "480", "720", "1080", "best"}
AUDIO_QUALITIES = {"128", "192", "320", "best"}

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".m4v",
    ".avi",
    ".ts",
    ".ogv",
    ".flv",
    ".3gp",
}


class YtDlpOptions(TypedDict, total=False):
    format: str
    merge_output_format: str
    outtmpl: str
    ffmpeg_location: str
    noplaylist: bool
    restrictfilenames: bool
    quiet: bool
    no_warnings: bool
    postprocessors: list[dict[str, str]]


FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# Avoid blocking FastAPI's event loop during media processing.
_download_pool = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="clipfetch-download",
)


class VideoDownloadError(Exception):
    """Raised when a media download cannot be completed."""


def validate_download_request(
    url: str,
    media_type: str,
    quality: str,
) -> None:
    """Validate the basic URL, media type, and quality choices."""

    if media_type not in {"video", "mp3"}:
        raise VideoDownloadError("Unsupported media type.")

    allowed_qualities = (
        VIDEO_QUALITIES if media_type == "video"
        else AUDIO_QUALITIES
    )

    if quality not in allowed_qualities:
        raise VideoDownloadError(
            f"Unsupported {media_type} quality."
        )

    if not isinstance(url, str) or len(url) > 4096:
        raise VideoDownloadError("Invalid URL.")

    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname

        # Accessing .port also detects malformed port values.
        _ = parsed.port
    except ValueError as exc:
        raise VideoDownloadError("Invalid URL.") from exc

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise VideoDownloadError(
            "URL must be a valid HTTP or HTTPS URL."
        )

    hostname = hostname.rstrip(".").lower()

    if (
        hostname == "localhost"
        or hostname.endswith(".localhost")
        or hostname.endswith(".local")
    ):
        raise VideoDownloadError(
            "Local network URLs are not allowed."
        )

    # Reject private/reserved IP literals in the submitted URL.
    # This alone is not complete SSRF protection for arbitrary URLs.
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None

    if address is not None and not address.is_global:
        raise VideoDownloadError(
            "Private or non-public IP addresses are not allowed."
        )


def _safe_title(title: str) -> str:
    """Create a filesystem-safe filename from the source title."""

    # Remove URL text if a source includes it in the title.
    title = re.sub(
        r"(?:https?://|www\.)\S+",
        "",
        title,
        flags=re.IGNORECASE,
    )

    # Remove characters forbidden in Windows filenames.
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title)
    title = re.sub(r"\s+", " ", title).strip(" .")

    # Windows reserves certain device names, even with extensions.
    if title.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        title = f"_{title}"

    # Leave room for the extension and collision suffix.
    title = title[:180].rstrip(" .")

    return title or "download"


def _publish_file(source: Path) -> Path:
    """Publish a completed file under an unguessable, app-owned name."""
    while True:
        destination = MEDIA_DIR / (
            f"{uuid.uuid4().hex}{source.suffix.lower()}"
        )

        try:
            os.link(source, destination)
        except FileExistsError:
            continue

        try:
            source.unlink()
            # Retention starts when processing completes, not at first fragment.
            os.utime(destination, None)
            return destination
        except OSError:
            try:
                destination.unlink()
            except OSError:
                logger.exception("Could not remove an unpublished media file.")
            raise


def _download_media(
    url: str,
    media_type: MediaType,
    quality: str,
) -> dict[str, Any]:
    """Perform the blocking yt-dlp download and finalize its file."""

    validate_download_request(url, media_type, quality)
    lease = acquire_storage_lease(shared=True)
    if lease is None:
        raise VideoDownloadError("Media storage is temporarily unavailable.")

    with lease:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        temp_dir = (
            MEDIA_DIR
            / f"{WORKING_DIRECTORY_PREFIX}{uuid.uuid4().hex}"
        )
        temp_dir.mkdir()

        try:
            if media_type == "video":
                if quality == "best":
                    format_selector = "bestvideo*+bestaudio/best"
                else:
                    format_selector = (
                        f"bestvideo[height<={quality}]+bestaudio/"
                        f"best[height<={quality}]"
                    )

                options: YtDlpOptions = {
                    "format": format_selector,
                    "merge_output_format": "mp4",
                    "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
                    "ffmpeg_location": FFMPEG_EXE,
                    "noplaylist": True,
                    "restrictfilenames": True,
                    "quiet": True,
                    "no_warnings": True,
                }

            else:
                audio_quality = "0" if quality == "best" else quality

                options = {
                    "format": "bestaudio/best",
                    "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
                    "ffmpeg_location": FFMPEG_EXE,
                    "noplaylist": True,
                    "restrictfilenames": True,
                    "quiet": True,
                    "no_warnings": True,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": audio_quality,
                        }
                    ],
                }

            # yt-dlp's type stub requires every defaulted option.
            with yt_dlp.YoutubeDL(cast(Any, options)) as ydl:
                info = ydl.extract_info(url, download=True)

            source_title = info.get("title") if info else None
            if not isinstance(source_title, str) or not source_title:
                raise VideoDownloadError(
                    "The source did not provide a usable media title."
                )

            if media_type == "mp3":
                candidates = [
                    path for path in temp_dir.iterdir()
                    if path.is_file() and path.suffix.lower() == ".mp3"
                ]
            else:
                candidates = [
                    path for path in temp_dir.iterdir()
                    if path.is_file()
                    and path.suffix.lower() in VIDEO_EXTENSIONS
                ]

            if not candidates:
                raise VideoDownloadError(
                    "The download finished without a usable output file. "
                    "Check FFmpeg compatibility and available formats."
                )

            # Normally there is one final output file. Prefer the
            # largest candidate if a source leaves multiple media files.
            completed_file = max(
                candidates,
                key=lambda path: path.stat().st_size,
            )

            file_size = completed_file.stat().st_size
            if file_size == 0:
                raise VideoDownloadError(
                    "The downloaded file is empty."
                )
            title = _safe_title(source_title)
            final_path = _publish_file(completed_file)

            return {
                "message": "Download successful!",
                "filename": f"{title}{final_path.suffix}",
                "title": source_title,
                "media_type": media_type,
                "quality_requested": quality,
                "duration": info.get("duration"),
                "extractor": info.get("extractor"),
                "file_size": file_size,
                "filepath": str(final_path),
            }

        except VideoDownloadError:
            raise
        except Exception as exc:
            logger.exception("Video download or processing failed.")
            raise VideoDownloadError(
                "The media could not be downloaded or processed. "
                "Check the URL, available formats and FFmpeg support."
            ) from exc
        finally:
            shutil.rmtree(temp_dir)


async def download_media(
    url: str,
    media_type: MediaType = "video",
    quality: str = "best",
) -> dict[str, Any]:
    """Download media without blocking the FastAPI event loop."""

    loop = asyncio.get_running_loop()

    try:
        return await loop.run_in_executor(
            _download_pool,
            _download_media,
            url,
            media_type,
            quality,
        )
    except VideoDownloadError:
        raise
    except Exception as exc:
        raise VideoDownloadError(
            "An unexpected download error occurred."
        ) from exc