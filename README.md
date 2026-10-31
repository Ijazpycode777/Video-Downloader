# ClipFetch

ClipFetch is an authenticated video/audio downloader API built with FastAPI, PostgreSQL, SQLAlchemy, Alembic, and yt-dlp. This repository contains the API and its deployment configuration; it does not contain a separate browser frontend.

## Features

- Invite-only registration, password login, and OAuth2 bearer authentication.
- Signed JWT access tokens using **HS256** with a fixed **30-minute** lifetime.
- Video downloads (360p, 480p, 720p, 1080p, or best) and MP3 extraction.
- Authenticated `POST /downloads/` returns the media as a normal attachment.
- Per-user download history stores the delivered filename and timestamp, not a job or file identifier.
- Temporary media is deleted after a successful response or expires after 30 minutes.
- PostgreSQL schema managed by Alembic; the migration preserves prior completed filenames.
- Docker Compose deployment with PostgreSQL, persistent database storage, and health checks.

## Run locally

Requirements: Python 3.14+, [uv](https://docs.astral.sh/uv/), and PostgreSQL. yt-dlp uses the FFmpeg binary provided by `imageio-ffmpeg`.

1. Create a local environment file and set strong secrets:

   ```powershell
   Copy-Item .env.example .env
   ```

   Set `DATABASE_URL` to a PostgreSQL async URL, `JWT_SECRET_KEY` to a unique random value of at least 32 bytes, `INVITE_CODE` to a private registration code, and `COOKIE_ENCRYPTION_KEY` to a Fernet key. Generate a Fernet key with `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Store it in a proper secret manager or protected environment setting. Do not use example values in a real deployment.

2. Install and migrate:

   ```powershell
   uv sync
   uv run python -m alembic upgrade head
   ```

3. Start the API:

   ```powershell
   uv run uvicorn main:app --host 0.0.0.0 --port 8000
   ```

OpenAPI documentation is available at `http://localhost:8000/docs`; `GET /health` checks database connectivity.

## Deploy with Docker Compose

Copy `.env.example` to `.env`, replace all example credentials, and start the services:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
```

The API container applies Alembic migrations before starting Uvicorn. PostgreSQL data and the temporary media directory use separate Docker volumes. For Compose, choose a database password that is URL-safe because it is embedded in the async database URL. Back up the database volume using your normal PostgreSQL backup process.

For a managed container platform, build the `ClipFetch` directory using the included `Dockerfile` and provide `DATABASE_URL`, `JWT_SECRET_KEY`, `INVITE_CODE`, `COOKIE_ENCRYPTION_KEY`, and a writable `CLIPFETCH_DOWNLOAD_DIR`. The database must be PostgreSQL and accessible before container startup. Set the platform's `PORT` as usual. Local media storage is ephemeral unless the platform mounts a persistent volume; downloads are still returned directly to the requesting user.

## Configuration

| Variable | Required | Description |
|---|---:|---|
| `DATABASE_URL` | Yes | PostgreSQL URL; `postgres://` and `postgresql://` are normalized to `postgresql+asyncpg://`. |
| `JWT_SECRET_KEY` | Yes | Unique signing secret, at least 32 UTF-8 bytes. |
| `INVITE_CODE` | Yes | Shared code required to register an account. |
| `COOKIE_ENCRYPTION_KEY` | For cookie import | Fernet key used to encrypt each user's provider cookies at rest. Back it up securely; changing it without re-encrypting stored cookies makes them unreadable. |
| `JWT_ALGORITHM` | No | Fixed to `HS256`; other values are rejected. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | No | Fixed to 30 minutes; other values are rejected. |
| `CLIPFETCH_DOWNLOAD_DIR` | No | Writable temporary media directory; defaults to `downloads`. |

## API flow

1. `POST /auth/register` creates an account using the invite code.
2. Authenticate at `POST /auth/token` using OAuth2 form fields (`username` accepts an email address or username) or `POST /auth/login` using JSON credentials. Both issue the same HS256 bearer JWT.
3. Send `Authorization: Bearer <token>` to `POST /downloads/`. The endpoint completes yt-dlp/FFmpeg processing and responds with the file attachment; no job ID or follow-up file URL is returned.
4. `GET /downloads/history` returns the authenticated user's filenames and download timestamps. It supports `limit` (1–100) and `offset` pagination.
5. `GET /auth/me` returns the authenticated profile.

### YouTube authentication cookies

ClipFetch does not create Google sessions or implement a YouTube OAuth flow that can be used by yt-dlp. Do not provide Google passwords. When YouTube explicitly returns yt-dlp's supported sign-in challenge, ClipFetch retries once with that authenticated user's cookies, if configured. Public downloads never use cookies. Other providers and generic network errors never trigger cookie authentication.

If needed, each user can export their own **Netscape-format** YouTube cookies from their browser and upload the file using `POST /auth/me/cookies/youtube` as multipart field `file`. Export only YouTube cookies; the API rejects cookie files containing other domains and requires a recognized YouTube authentication cookie. The cookie contents are encrypted using `COOKIE_ENCRYPTION_KEY`, scoped to the authenticated user and the YouTube provider, and never returned. Use `GET /auth/me/cookies/youtube/status` to check state and next persistent-cookie expiration; `DELETE /auth/me/cookies/youtube` removes the stored credentials.

Persistent cookie expiry timestamps are retained as supplied. Expired cookies are omitted from yt-dlp's temporary cookie file. Session cookies remain marked as session cookies (no fabricated expiry); YouTube rejection marks the stored credentials as requiring reauthentication. ClipFetch does not automatically refresh cookies because no supported yt-dlp-compatible provider refresh mechanism is implemented. Users must export and import a replacement cookie file when required. Cookies are written to a unique, permission-restricted temporary file only for a single retry and removed in a `finally` block. Cookie authentication is not used to bypass age, paid, regional, private-video, or DRM restrictions.

The download history is metadata only. The media file is temporary, is deleted after a complete response, and otherwise expires after 30 minutes. Since the API responds directly with the file, the caller must keep the original POST response to save it; a history entry is not a file retrieval link.

## Error responses

Errors use a JSON `detail` message describing the actionable failure. Invalid request data returns `422`; unsupported or unavailable source media returns `422`; invalid URL/quality requests return `400`; database conflicts return `409`; unavailable storage or database services return `503`; and unexpected server failures return `500`. yt-dlp and FFmpeg errors are translated into safe messages rather than returning raw provider output, SQL, or credentials. Detailed exception traces remain in server logs for diagnosis.

## Security and operations

- Use HTTPS and unique production credentials. Keep `.env` out of source control.
- Each history query is scoped to the authenticated user. Passwords are stored as Argon2 hashes.
- URL validation blocks common local/private-address cases but is not a complete SSRF defense. Restrict outbound network access at the deployment boundary.
- Set platform-level request timeouts, memory/disk limits, and rate/concurrency limits appropriate for the workload.
- The temporary media directory is local to each container. This is compatible with direct POST delivery, but does not make media durable or shared across replicas.

No license has been specified.
