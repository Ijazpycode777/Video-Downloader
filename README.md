ClipFetch

A private video and audio downloader built with FastAPI and PostgreSQL.

ClipFetch is a backend API that allows registered users to request video downloads or extract audio as MP3. It uses "yt-dlp" to retrieve media and FFmpeg to process it.

«Version: 0.0.1 — Early development»

Features

- User authentication: Password hashing and JWT-based authentication.
- Video downloads: Select video quality, including 360p, 480p, 720p, 1080p, or best available.
- MP3 extraction: Extract audio with configurable quality options.
- PostgreSQL database: Persist users and download-job records.
- Database migrations: Manage schema changes using Alembic.
- Download tracking: Store job status, media metadata, and error information.
- Direct media delivery: Return completed downloads as authenticated file attachments.
- Temporary storage: Remove delivered media immediately and expire undelivered media after 30 minutes.
- Asynchronous API: Built with FastAPI and SQLAlchemy's asynchronous database support.

Tech Stack

Technology| Purpose
Python| Application language
FastAPI| REST API framework
PostgreSQL| Persistent database
SQLAlchemy| ORM and asynchronous database access
Alembic| Database migrations
Pydantic Settings| Configuration management
Argon2| Password hashing
PyJWT| JWT authentication
yt-dlp| Media downloading
FFmpeg| Media processing and audio extraction
uv| Python dependency and environment management

Project Structure

ClipFetch/
├── main.py
├── pyproject.toml
├── uv.lock
├── alembic.ini
├── migrations/
│   ├── env.py
│   └── versions/
└── app/
    ├── config.py
    ├── engine.py
    ├── models.py
    ├── security.py
    ├── routers/
    │   ├── auth.py
    │   └── downloads.py
    └── services/
        ├── storage.py
        └── video.py

Requirements

- Python version specified by ".python-version"
- PostgreSQL
- FFmpeg
- Git
- uv

Getting Started

1. Clone the repository

git clone https://github.com/YOUR_USERNAME/ClipFetch.git
cd ClipFetch

Replace "YOUR_USERNAME" with your GitHub username.

2. Install dependencies

uv sync

3. Configure environment variables

Create a ".env" file in the project root.

Configure the following variables with your own values:

DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/DATABASE
JWT_SECRET_KEY=YOUR_STRONG_RANDOM_SECRET
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
INVITE_CODE=YOUR_PRIVATE_INVITE_CODE

Use the actual variable names and formats expected by the application configuration. Never commit ".env" or expose real credentials in public repositories.

4. Apply database migrations

Ensure PostgreSQL is running and the configured database exists.

uv run python -m alembic upgrade head

5. Start the API

uv run uvicorn main:app --reload

The development server should be available at:

- API root: "http://127.0.0.1:8000/"
- Health check: "http://127.0.0.1:8000/health"
- Interactive API documentation: "http://127.0.0.1:8000/docs"

API Overview

Endpoint| Purpose
"GET /"| API welcome message
"GET /health"| Health check
Authentication routes| User registration and login
"POST /downloads/"| Download and return a media attachment
"GET /downloads/{job_id}/file"| Retrieve a completed attachment, subject to authorization

Consult the interactive Swagger documentation at "/docs" for the exact request schemas and authentication requirements.
For Swagger's OAuth2 login, enter either your account email or username in the "username" field, along with your password.

Temporary media and Render deployment

- `POST /downloads/` returns the media bytes with `Content-Disposition: attachment`; `X-Download-Job-ID` identifies the persisted job. The endpoint still waits for yt-dlp and FFmpeg to finish before responding.
- The authenticated `GET /downloads/{job_id}/file` endpoint remains available. A media file is removed after delivery, or expires 30 minutes after processing completes if it is never delivered. A job's status and metadata remain in PostgreSQL; its file may no longer be available, in which case file retrieval returns 404.
- Media and partial downloads are kept under `.clipfetch-media` inside `CLIPFETCH_DOWNLOAD_DIR` (default: `downloads`). Startup and 30-second periodic cleanup remove only files created by ClipFetch once they reach the 30-minute expiry. Active downloads and transfers hold a filesystem lock so cleanup cannot remove files in use. Abandoned work directories are eligible for cleanup after 30 minutes.
- No Render setting or persistent disk is required for this direct-response flow. The Render filesystem is ephemeral, so media is not expected to survive restarts. `CLIPFETCH_DOWNLOAD_DIR` remains optional and, if set, must point to a writable directory.
- Workers sharing a filesystem coordinate cleanup with OS file locks. Separate Render instances have separate ephemeral filesystems; direct POST delivery stays on the instance handling that request, but a delayed GET handled by a different instance cannot access another instance's local file. Use one instance or shared storage that supports file locking if cross-instance retrieval is required.

Security Notes

- Store credentials and signing keys in environment variables.
- Use strong, unique JWT signing secrets.
- Keep password hashes instead of plaintext passwords.
- Restrict download-job and file access to the owning user.
- URL checks reject common local and private-address cases but are not complete SSRF protection. Restrict server-side network access separately.
- Apply download-size limits, concurrency controls, and storage quotas before accepting unrestricted public traffic.
- Use HTTPS in production.

Current Status

ClipFetch v0.0.1 is an early-stage backend project. Local development and deployment readiness are separate milestones; production operation requires appropriate security controls, download limits, and monitoring.

License

No license has been specified yet. All rights are reserved unless a license is added to this repository.
