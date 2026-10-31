FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY main.py alembic.ini ./
COPY app ./app
COPY migrations ./migrations

RUN useradd --system --create-home --shell /usr/sbin/nologin clipfetch \
    && mkdir -p /var/lib/clipfetch/downloads \
    && chown -R clipfetch:clipfetch /var/lib/clipfetch

USER clipfetch
EXPOSE 8000

CMD ["sh", "-c", "/opt/venv/bin/alembic upgrade head && exec /opt/venv/bin/uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
