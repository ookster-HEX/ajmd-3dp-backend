FROM python:3.11-slim-bookworm

# Install uv from the official Astral image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install system dependencies required by headless OrcaSlicer
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    ca-certificates \
    xvfb \
    libgl1 \
    libegl1 \
    libglu1-mesa \
    libgtk-3-0 \
    libwebkit2gtk-4.0-37 \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    libosmesa6 \
    libxkbcommon0 \
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Download and extract OrcaSlicer AppImage
ARG ORCA_VERSION=v2.2.0
ARG ORCA_FILENAME=OrcaSlicer_Linux_V2.2.0.AppImage

RUN wget -q "https://github.com{ORCA_VERSION}/${ORCA_FILENAME}" -O /tmp/orcaslicer.AppImage \
    && chmod +x /tmp/orcaslicer.AppImage \
    && cd /tmp && ./orcaslicer.AppImage --appimage-extract \
    && mv /tmp/squashfs-root /opt/orcaslicer \
    && printf '#!/bin/sh\nexec xvfb-run -a /opt/orcaslicer/AppRun "$@"\n' > /usr/local/bin/orca-slicer \
    && chmod +x /usr/local/bin/orca-slicer \
    && rm /tmp/orcaslicer.AppImage

WORKDIR /app

# Enable bytecode compilation and copy mode for uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Copy dependency definitions first for Docker layer caching
# (uv.lock* uses a wildcard so the build still succeeds if you haven't committed a lockfile yet)
COPY pyproject.toml uv.lock* ./

# Install dependencies without installing the project itself yet
RUN uv sync --no-install-project --no-dev

# Copy the rest of your application code
COPY . .

# Sync the project into the virtual environment
RUN uv sync --no-dev

# Put the virtual environment on PATH so uvicorn and python work directly
ENV PATH="/app/.venv/bin:$PATH"

# Create mount points
RUN mkdir -p /data/db /data/profiles

EXPOSE 8888

# Run FastAPI via uvicorn on port 8888 (adjust "main:app" if your entry module differs)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888"]
