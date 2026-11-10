FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# ffmpeg: merges video+audio streams. deno: JS runtime yt-dlp needs for YouTube.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

CMD ["python", "-u", "main.py"]
