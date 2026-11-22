# Use Python 3.11 slim Debian bookworm base image
FROM python:3.11-slim-bookworm

# Install system dependencies required by OrcaSlicer (EGL/GL/GTK/FUSE/curl) and uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libegl1 \
    libgl1 \
    libglu1-mesa \
    libgtk-3-0 \
    libwebkit2gtk-4.0-37 \
    libfuse2 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install OrcaSlicer Linux AppImage/binary into /usr/local/bin/orca-slicer
ARG ORCA_VERSION=v2.2.0
RUN curl -L "https://github.com/SoftFever/OrcaSlicer/releases/download/${ORCA_VERSION}/OrcaSlicer_Linux_V2.2.0.AppImage" -o /opt/orca-slicer.AppImage \
    && chmod +x /opt/orca-slicer.AppImage \
    && cd /opt && /opt/orca-slicer.AppImage --appimage-extract \
    && rm /opt/orca-slicer.AppImage \
    && printf '#!/bin/sh\nxvfb-run -a /opt/squashfs-root/AppRun "$@"\n' > /usr/local/bin/orca-slicer \
    && chmod +x /usr/local/bin/orca-slicer

WORKDIR /app

# Copy project configuration and install Python dependencies via uv
COPY pyproject.toml ./
RUN uv sync --no-dev

# Copy application source and admin helper script
COPY main.py make_admin.py ./

# Create persistent directories for SQLite DB, user uploads, samples, and slicer profiles
RUN mkdir -p /app/data /app/storage/useruploads /app/storage/samples /app/profiles

EXPOSE 8000

# Run FastAPI via uvicorn inside the uv virtual environment
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]