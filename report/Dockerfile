# chase report service (Cloud Run): violation evidence -> PDF complaint -> email.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
ARG DEV=0
RUN if [ "$DEV" = "1" ]; then uv sync --frozen --no-install-project; \
    else uv sync --frozen --no-dev --no-install-project; fi
COPY src ./src
RUN if [ "$DEV" = "1" ]; then uv sync --frozen; else uv sync --frozen --no-dev; fi

RUN useradd --system --uid 1000 --create-home chase
USER chase
ENV PORT=8080
EXPOSE 8080

# One request at a time (exactly-once email; ADR 0005). Pub/Sub's ack deadline is 600 s.
CMD ["sh", "-c", "exec gunicorn -b :$PORT -w 1 --threads 1 --timeout 600 'chase_report.app:create_from_env()'"]
