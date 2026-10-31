import asyncio
import ipaddress
import os
import re
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, NoReturn, TypedDict, cast
from urllib.parse import urlsplit
import logging
import imageio_ffmpeg
import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError, PostProcessingError

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
    cookiefile: str


FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# Avoid blocking FastAPI's event loop during media processing.
_download_pool = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="clipfetch-download",
)


class VideoDownloadError(Exception):
    """Raised when a media download cannot be completed."""

    status_code = 400


class MediaSourceError(VideoDownloadError):
    status_code = 422


class MediaProcessingError(VideoDownloadError):
    status_code = 422


class MediaStorageError(VideoDownloadError):
    status_code = 503


class InternalMediaError(VideoDownloadError):
    status_code = 500


class ProviderAuthenticationRequiredError(VideoDownloadError):
    status_code = 401

    def __init__(self) -> None:
        super().__init__(
            "The YouTube provider requires an authenticated user session."
        )
        self.provider = "youtube"


class ProviderAuthenticationRejectedError(
    ProviderAuthenticationRequiredError
):
    pass


class RestrictedMediaError(VideoDownloadError):
    status_code = 403

    def __init__(self) -> None:
        super().__init__(
            "The provider reports restricted content. ClipFetch will not use "
            "cookies to bypass age, paid, regional, private-video, or DRM "
            "restrictions."
        )


def _youtube_provider_for_url(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
    except ValueError:
        return None
    if hostname is None:
        return None
    hostname = hostname.lower().rstrip(".")
    if hostname == "youtube.com" or hostname.endswith(".youtube.com"):
        return "youtube"
    return None


def _raise_source_error(
    exc: Exception,
    *,
    url: str,
    has_cookies: bool,
) -> NoReturn:
    message = str(exc).casefold().replace("’", "'")
    provider = _youtube_provider_for_url(url)

    restricted_markers = (
        "confirm your age",
        "age-restricted",
        "members-only",
        "members only",
        "private video",
        "not available in your country",
        "not available in your region",
        "drm",
    )
    if provider == "youtube" and any(
        marker in message for marker in restricted_markers
    ):
        raise RestrictedMediaError() from exc

    if (
        provider == "youtube"
        and "sign in to confirm you're not a bot" in message
    ):
        if has_cookies:
            raise ProviderAuthenticationRejectedError() from exc
        raise ProviderAuthenticationRequiredError() from exc

    if "429" in message or "too many requests" in message:
        raise MediaSourceError(
            "The source provider rate-limited this request. Wait before "
            "retrying; cookies will not be used to bypass rate limits."
        ) from exc
    raise MediaSourceError(
        "yt-dlp could not retrieve this source. It may be unavailable, "
        "unsupported, or experiencing a network problem. Verify the URL "
        "and try again."
    ) from exc


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
    cookie_data: bytes | None = None,
) -> dict[str, Any]:
    """Perform the blocking yt-dlp download and finalize its file."""

    validate_download_request(url, media_type, quality)
    try:
        lease = acquire_storage_lease(shared=True)
    except OSError as exc:
        raise MediaStorageError(
            "ClipFetch could not access the media storage directory. "
            "Verify that it exists and is writable."
        ) from exc
    if lease is None:
        raise MediaStorageError(
            "Media storage is busy with another operation. Please retry."
        )

    with lease:
        temp_dir = (
            MEDIA_DIR
            / f"{WORKING_DIRECTORY_PREFIX}{uuid.uuid4().hex}"
        )
        cookie_path: Path | None = None

        try:
            MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            temp_dir.mkdir()
            if cookie_data is not None:
                cookie_path = temp_dir / "youtube-cookies.txt"
                cookie_descriptor = os.open(
                    cookie_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(cookie_descriptor, "wb") as cookie_file:
                    cookie_file.write(cookie_data)
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

            if cookie_path is not None:
                options["cookiefile"] = str(cookie_path)

            # yt-dlp's type stub requires every defaulted option.
            try:
                with yt_dlp.YoutubeDL(cast(Any, options)) as ydl:
                    info = ydl.extract_info(url, download=True)
            except PostProcessingError as exc:
                logger.warning("FFmpeg media post-processing failed.")
                raise MediaProcessingError(
                    "FFmpeg could not process this media. Check that the "
                    "source has a compatible format and retry."
                ) from exc
            except (
                DownloadError,
                ExtractorError,
            ) as exc:
                logger.warning(
                    "yt-dlp source retrieval failed (%s).",
                    type(exc).__name__,
                )
                _raise_source_error(
                    exc,
                    url=url,
                    has_cookies=cookie_data is not None,
                )

            source_title = info.get("title") if info else None
            if not isinstance(source_title, str) or not source_title:
                raise MediaSourceError(
                    "The source did not provide a usable media title. "
                    "Try another video URL."
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
                raise MediaProcessingError(
                    "The download finished without a usable output file. "
                    "Check that the requested format is available and "
                    "FFmpeg can process it."
                )

            # Normally there is one final output file. Prefer the
            # largest candidate if a source leaves multiple media files.
            completed_file = max(
                candidates,
                key=lambda path: path.stat().st_size,
            )

            file_size = completed_file.stat().st_size
            if file_size == 0:
                raise MediaProcessingError(
                    "The source returned an empty media file. Check the "
                    "source and try again."
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
        except OSError as exc:
            logger.exception("Media storage operation failed.")
            raise MediaStorageError(
                "ClipFetch could not read or write the media file. Verify "
                "that the configured download directory has free space and "
                "is writable."
            ) from exc
        except Exception as exc:
            logger.exception("Video download or processing failed.")
            raise InternalMediaError(
                "An unexpected error occurred while preparing this media. "
                "Retry the request; contact the service administrator if "
                "the problem continues."
            ) from exc
        finally:
            cleanup_error: OSError | None = None
            if cookie_path is not None:
                try:
                    cookie_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.exception(
                        "Could not remove temporary YouTube credentials."
                    )
                    cleanup_error = exc
            try:
                shutil.rmtree(temp_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.exception(
                    "Could not remove temporary media directory %s.",
                    temp_dir,
                )
                if cleanup_error is None:
                    cleanup_error = exc
            if cleanup_error is not None:
                raise MediaStorageError(
                    "ClipFetch could not remove temporary download or "
                    "credential files. Securely remove the temporary media "
                    "directory before restarting."
                ) from cleanup_error


async def download_media(
    url: str,
    media_type: MediaType = "video",
    quality: str = "best",
    cookie_data: bytes | None = None,
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
            cookie_data,
        )
    except VideoDownloadError:
        raise
    except Exception as exc:
        logger.exception("Media download worker failed unexpectedly.")
        raise InternalMediaError(
            "The media download worker failed unexpectedly. Please retry "
            "the request."
        ) from exc