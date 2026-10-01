FROM python:3.12-slim-bookworm

# Firefox runtime libraries needed by Camoufox
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libgtk-3-0 libdbus-glib-1-2 libxt6 libasound2 libx11-xcb1 \
        libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libatk1.0-0 libatk-bridge2.0-0 libxkbcommon0 libxshmfence1 \
        fonts-liberation curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -m camoufox fetch

COPY server.py .

ENV PYTHONUNBUFFERED=1
EXPOSE 8191
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s CMD curl -fs http://localhost:8191/health || exit 1

CMD ["python", "server.py"]
