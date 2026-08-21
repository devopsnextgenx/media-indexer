FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    atomicparsley \
    curl \
    git \
    nodejs \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://deno.land/install.sh | sh && \
    cp /root/.deno/bin/deno /usr/local/bin/deno

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml README.md uv.lock* ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

COPY config.yml ./
COPY src/ ./src/
COPY static/ ./static/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen && \
    uv pip install --upgrade yt-dlp

EXPOSE 2345

CMD ["uv", "run", "fastapi", "run", "src/media_indexer/main.py", "--host", "0.0.0.0", "--port", "2345"]