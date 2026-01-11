# [START FILE: abs-kosync-enhanced/Dockerfile]
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FLASK_APP=web_server.py \
    PYTHONPATH="/app/src"

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
    beautifulsoup4

# 3. Create directories
RUN mkdir -p /app/src /app/templates /app/static /data/audio_cache /data/logs /data/transcripts

# 4. Copy Application Code
COPY main.py /app/src/main.py
COPY storyteller_db.py /app/src/storyteller_db.py
COPY storyteller_api.py /app/src/storyteller_api.py
COPY transcriber.py /app/src/transcriber.py
COPY logging_utils.py /app/src/logging_utils.py
COPY ebook_utils.py /app/src/ebook_utils.py
COPY api_clients.py /app/src/api_clients.py
COPY json_db.py /app/src/json_db.py
COPY hardcover_client.py /app/src/hardcover_client.py
COPY booklore_client.py /app/src/booklore_client.py
COPY di_container.py /app/src/di_container.py
COPY autowiring.py /app/autowiring.py

# Copy sync client modules
COPY src/sync_clients/ /app/src/sync_clients/

COPY web_server.py /app/web_server.py
COPY templates/ /app/templates/
COPY static/ /app/static/

COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 5757

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD curl -f http://localhost:5757/ || exit 1

CMD ["/app/start.sh"]
# [END FILE]
