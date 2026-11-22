FROM python:3.11-slim-bookworm

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

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create mount points
RUN mkdir -p /data/db /data/profiles

EXPOSE 8888

# Run FastAPI via uvicorn on port 8888 (adjust "main:app" if needed)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8888"]
