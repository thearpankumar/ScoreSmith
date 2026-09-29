"""Seed the database with the Cycle 1 scenario catalogue.

Usage (from backend/, with DATABASE_URL set to a psycopg v3 Postgres URL and migrations
already applied via `alembic upgrade head`):

    python -m app.scripts.seed
    python -m app.scripts.seed --reset

Idempotency: by default, the script checks for a marker user
(email=seed-marker@qualityscorecard.local). If present, it assumes the DB is already
seeded and exits without making changes. Pass `--reset` to first TRUNCATE every
app table (RESTART IDENTITY CASCADE) and reseed from scratch. This is the "clear and
reseed" strategy (chosen over "skip existing rows one by one" because the scenario
catalogue is small, relational, and easiest to reason about as an atomic all-or-nothing
dataset rather than partially patched).
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.db import SyncSessionLocal, sync_engine
from app.scripts.generate_scenarios import SEED_MARKER_EMAIL, run_all_scenarios

APP_TABLES = [
    "audit_log",
    "chat_messages",
    "chat_sessions",
    "evaluation_kpi_results",
    "evaluations",
    "scorecard_embeddings",
    "kpi_guidelines",
    "kpi_nodes",
    "scorecard_versions",
    "scorecards",
    "users",
]


def _already_seeded() -> bool:
    with SyncSessionLocal() as session:
        row = session.execute(
            text("SELECT 1 FROM users WHERE email = :email"), {"email": SEED_MARKER_EMAIL}
        ).first()
        return row is not None


def _reset() -> None:
    print("Resetting: truncating all app tables (RESTART IDENTITY CASCADE)...")
    with sync_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} RESTART IDENTITY CASCADE;"))
    print("Reset complete.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Cycle 1 scenario catalogue.")
    parser.add_argument(
        "--reset", action="store_true", help="Truncate all app tables before reseeding."
    )
    args = parser.parse_args(argv)

    if args.reset:
        _reset()
    elif _already_seeded():
        print(
            f"Database already seeded (marker user '{SEED_MARKER_EMAIL}' exists). "
            "Skipping. Pass --reset to reseed from scratch."
        )
        return 0

    with SyncSessionLocal() as session:
        report = run_all_scenarios(session)

    report.print_summary()
    n_failed = sum(1 for r in report.results if r.status == "FAILED")
    return 1 if n_failed else 0


if __name__ == "__main__":
    sys.exit(main())
