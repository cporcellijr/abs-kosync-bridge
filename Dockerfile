# [START FILE: abs-kosync-enhanced/Dockerfile]
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=web_server.py \
    PYTHONPATH="/app"

WORKDIR /app

# 1. Install System Dependencies
# FFmpeg with full codec support for audio conversion
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    libavcodec-extra \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python Dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    flask \
    requests \
    lxml \
    rapidfuzz \
    schedule \
    faster-whisper \
    EbookLib \
    beautifulsoup4 \
    dependency-injector

# 3. Create directories
RUN mkdir -p /app/src /app/templates /app/static /data/audio_cache /data/logs /data/transcripts

# 4. Copy Application Code
copy src/ /app/src/
COPY templates/ /app/templates/
COPY static/ /app/static/

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 5757

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:5757/ || exit 1

CMD ["/app/start.sh"]
# [END FILE]
