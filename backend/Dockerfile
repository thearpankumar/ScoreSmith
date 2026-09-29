FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

# libpq5 is required at runtime by psycopg[binary]
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY . .

RUN pip install --no-cache-dir .

EXPOSE 8000

# --reload is acceptable for dev; the compose file bind-mounts ../backend:/app so
# edits on the host are picked up. PYTHONPATH=/app + WORKDIR /app ensure "app.main"
# resolves to the live mounted source rather than the copy baked into the image.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
