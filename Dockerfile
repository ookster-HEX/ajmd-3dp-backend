FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Install uv from the official Astral image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install system dependencies required by headless OrcaSlicer on Ubuntu 24.04
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    xvfb \
    libgl1 \
    libegl1 \
    libglu1-mesa \
    libgtk-3-0 \
    libwebkit2gtk-4.1-0 \
    libgstreamer1.0-0 \
    libgstreamer-plugins-base1.0-0 \
    libosmesa6 \
    libxkbcommon0 \
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libgbm1 \
    libasound2t64 \
    libmspack0t64 \
    libbz2-1.0 \
    && rm -rf /var/lib/apt/lists/*

# Assemble the URL at build time so copy/paste tools cannot strip the path
RUN URL=$(printf '%s/%s/%s' \
      'https://github.com' \
      'OrcaSlicer/OrcaSlicer/releases/download/v2.5.2' \
      'OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.5.2.AppImage') \
    && curl -fSL "$URL" -o /tmp/orcaslicer.AppImage \
    && chmod +x /tmp/orcaslicer.AppImage \
    && cd /tmp && ./orcaslicer.AppImage --appimage-extract \
    && mv /tmp/squashfs-root /opt/orcaslicer \
    && printf '#!/bin/sh\nexec xvfb-run -a /opt/orcaslicer/AppRun "$@"\n' > /usr/local/bin/orca-slicer \
    && chmod +x /usr/local/bin/orca-slicer \
    && rm /tmp/orcaslicer.AppImage

WORKDIR /app

# Enable bytecode compilation, copy mode, and let uv install Python 3.11
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.11

# Copy dependency definitions first for Docker layer caching
COPY pyproject.toml uv.lock* ./

# Install Python 3.11 and project dependencies
RUN uv sync --no-install-project --no-dev

# Copy the rest of your application code
COPY . .

# Sync the project into the virtual environment
RUN uv sync --no-dev

# Put the virtual environment on PATH
ENV PATH="/app/.venv/bin:$PATH"

# Create mount points
RUN mkdir -p /data/db /data/profiles

EXPOSE 8888

# Run FastAPI via uvicorn on port 8888 (adjust "main:app" if needed)
CMD ["uv", "fastapi", "run", "--host", "0.0.0.0", "--port", "8888"]
