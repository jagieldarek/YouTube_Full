FROM denoland/deno:bin AS deno

FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp potrzebuje środowiska JavaScript, żeby rozwiązywać zabezpieczenia YouTube.
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --uid 1000 --no-create-home --home-dir /tmp app \
    && mkdir -p /downloads && chown app /downloads
USER app

ENV DOWNLOAD_DIR=/downloads \
    HOME=/tmp \
    DENO_DIR=/tmp/deno \
    PYTHONUNBUFFERED=1

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
