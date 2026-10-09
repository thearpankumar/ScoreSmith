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

# The image runs in three roles (see infra/docker-compose.yml):
#   - migrate : `python -m app.scripts.migrate` - Alembic + checkpoint tables, a one-off job that runs ONCE per
#               release before anything else starts (it is no longer part of this CMD: several replicas booting
#               together raced on it).
#   - backend : this default CMD with ROLE=api - gunicorn supervising uvicorn workers; HTTP only. WEB_CONCURRENCY
#               sets the worker count; --graceful-timeout lets in-flight requests finish on SIGTERM.
#   - worker  : `python -m app.worker` with ROLE=worker - evaluation dispatcher + background chat turns.
# No --reload: long jobs must not be killed by a file change under the bind-mounted source. After editing
# backend code, restart the containers (`docker compose restart backend worker`).
CMD ["sh", "-c", "exec gunicorn app.main:app --worker-class uvicorn.workers.UvicornWorker --workers ${WEB_CONCURRENCY:-2} --bind 0.0.0.0:8000 --graceful-timeout 30 --timeout 120 --access-logfile -"]
