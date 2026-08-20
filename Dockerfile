FROM python:3.11-slim

# Install system dependencies (FFmpeg for stream merging/metadata, AtomicParsley for embedding thumbnails,
# curl/unzip/ca-certificates for Deno installation)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    atomicparsley \
    curl \
    git \
    nodejs \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Deno JS runtime so yt-dlp can execute YouTube JS extractors and solve challenge code
RUN curl -fsSL https://deno.land/install.sh | sh && \
    cp /root/.deno/bin/deno /usr/local/bin/deno

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml README.md uv.lock* ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Copy project definition
COPY config.yml ./
COPY src/ ./src/
COPY static/ ./static/

# Sync dependencies using uv and ensure yt-dlp is updated to the latest build
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen && \
    uv pip install --upgrade yt-dlp && \
    deno --version && \
    node --version

EXPOSE 2345

# Command to execute application
CMD ["uv", "run", "fastapi", "run", "src/media_indexer/main.py", "--host", "0.0.0.0", "--port", "2345"]