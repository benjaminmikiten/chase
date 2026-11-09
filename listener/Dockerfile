# chase listener — runs on the OptiPlex (linux/amd64; also builds on arm64).
FROM python:3.12-slim AS base

RUN apt-get update \
 && apt-get install -y --no-install-recommends libportaudio2 libsndfile1 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# YAMNet (MediaPipe audio classifier export) + AudioSet class map, pinned by SHA-256.
ARG YAMNET_URL=https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite
ARG YAMNET_SHA256=4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf
ARG CLASS_MAP_URL=https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv
ARG CLASS_MAP_SHA256=cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2
RUN mkdir -p /models \
 && curl -fsSL -o /models/yamnet.tflite "$YAMNET_URL" \
 && curl -fsSL -o /models/yamnet_class_map.csv "$CLASS_MAP_URL" \
 && echo "$YAMNET_SHA256  /models/yamnet.tflite" | sha256sum -c - \
 && echo "$CLASS_MAP_SHA256  /models/yamnet_class_map.csv" | sha256sum -c -

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 1000 --gid audio --create-home chase \
 && mkdir -p /data && chown chase:audio /data
USER chase
VOLUME ["/data"]
EXPOSE 9100

ENTRYPOINT ["chase"]
CMD ["run", "--config", "/config/chase.yaml", "--presets", "/config/presets"]
