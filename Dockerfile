FROM python:3.12-slim

# System dependencies:
#   adb    — push photos/videos to the Frameo frame via WiFi
#   ffmpeg — required for video processing (trim/scale/re-encode)
RUN apt-get update && apt-get install -y --no-install-recommends \
        adb \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy Python modules, the config template, and the entrypoint
COPY *.py ./
COPY config.yaml.example ./
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Pre-create runtime directories (will be mounted as volumes in docker-compose)
RUN mkdir -p inbox processed archive/failed logs data

STOPSIGNAL SIGTERM

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
