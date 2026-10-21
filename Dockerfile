# audio-normalizer: web UI, scheduler and normalizer in one container.
# Target: Synology DS918+ (Intel J3455, amd64) via Container Manager / docker compose.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    AUDIO_ROOT=/data/audio \
    VIDEO_ROOT=/data/video \
    PORT=8080 \
    TZ=Europe/Amsterdam

# ffmpeg: loudness analysis and lossless volume changes.
# mp3gain: lossless MP3 gain. aacgain: lossless AAC/M4A gain.
# If the aacgain package is not available, the build continues and AAC/M4A files are
# parked for a decision instead of being changed (see the README).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg mp3gain \
 && (apt-get install -y --no-install-recommends aacgain \
     || echo "WARNING: aacgain package not available; AAC gain disabled") \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8080'), timeout=4)"

CMD ["python", "-m", "app"]
