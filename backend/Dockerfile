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

# Applies pending Alembic migrations (idempotent; needs DATABASE_URL, which compose injects)
# and then starts the API WITHOUT --reload: chat turns now run as long-lived background tasks
# inside the server process, and an auto-reload (triggered by any edit under the bind-mounted
# source) would kill them mid-run. After editing backend code, restart the container instead
# (`docker compose restart backend`); startup recovery marks any interrupted turn as failed.
# A failing migration is logged but does not stop the API from starting.
CMD ["sh", "-c", "python -m alembic upgrade head || echo 'WARNING: alembic upgrade head failed - check DATABASE_URL/DB state'; exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
