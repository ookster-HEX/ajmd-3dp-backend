# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

# Install system dependencies required by OrcaSlicer (EGL/GL/GTK/xvfb) and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libegl1 \
    libgl1 \
    libglu1-mesa \
    libgtk-3-0 \
    libwebkit2gtk-4.0-37 \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Pin uv version for reproducible builds
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

# Install and extract OrcaSlicer Linux AppImage
ARG GH_REPO="SoftFever/OrcaSlicer"
ARG ORCA_TAG="v2.2.0"
ARG ORCA_FILE="OrcaSlicer_Linux_V2.2.0.AppImage"

RUN curl -fsSL "https://github.com{GH_REPO}/releases/download/${ORCA_TAG}/${ORCA_FILE}" -o /opt/orca-slicer.AppImage \
    && chmod +x /opt/orca-slicer.AppImage \
    && cd /opt && /opt/orca-slicer.AppImage --appimage-extract \
    && rm /opt/orca-slicer.AppImage \
    && printf '#!/bin/sh\nexec xvfb-run -a /opt/squashfs-root/AppRun "$@"\n' > /usr/local/bin/orca-slicer \
    && chmod +x /usr/local/bin/orca-slicer



WORKDIR /app

# Put the uv virtual environment directly on PATH
ENV PATH="/app/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install Python dependencies first for optimal layer caching
COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

# Create non-root user and persistent directories
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/data /app/storage/useruploads /app/storage/samples /app/profiles

# Copy application source with proper ownership
COPY --chown=appuser:appuser main.py make_admin.py ./
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8888

# Run uvicorn directly from the virtual environment
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888", "--proxy-headers"]
