"""Alembic migrations 0011 (scaling leases) and 0012 (auth + ownership): upgrade, data backfill, downgrade.

Runs in a throw-away Postgres SCHEMA (search_path of a subprocess) so the shared test database that every other
test truncates is never touched. Extensions (vector, ltree) live in `public` and stay visible through the path."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _plain_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1).split("?")[0]


@pytest.fixture()
def scratch(_require_database_url):
    schema = f"mig_{uuid.uuid4().hex[:10]}"
    base = os.environ["DATABASE_URL"].split("?")[0]
    dsn = _plain_dsn(base)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{schema}"')
        # Own (empty) version table first on the path, or alembic would find the shared DB's table in `public`
        # and believe this scratch schema is already at head.
        c.execute(f'CREATE TABLE "{schema}".alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY)')
    env = {**os.environ, "DATABASE_URL": f"{base}?options=-csearch_path%3D{schema},public"}

    def alembic(*args: str) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=150
        )
        assert proc.returncode == 0, proc.stderr[-2000:]

    def sql(query: str, params=None):
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(f'SET search_path TO "{schema}", public')
            cur = c.execute(query, params)
            return cur.fetchall() if cur.description else None

    def columns(table: str) -> set[str]:
        rows = sql(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        return {r[0] for r in rows}

    def tables() -> set[str]:
        return {r[0] for r in sql("SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (schema,))}

    yield alembic, sql, columns, tables
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')


def test_0011_and_0012_upgrade_backfill_and_downgrade(scratch) -> None:
    alembic, sql, columns, tables = scratch

    alembic("upgrade", "0010_ai_eval_pipeline")
    assert "idempotency_keys" not in tables()
    assert "lease_owner" not in columns("evaluations")

    # 0011: lease columns + idempotency table appear; chat turn lease columns too.
    alembic("upgrade", "0011_scaling_leases")
    assert {"lease_owner", "lease_expires_at", "heartbeat_at", "cancel_requested_at"} <= columns("evaluations")
    assert {"turn_message", "turn_lease_owner", "turn_cancel_requested_at"} <= columns("chat_sessions")
    assert "idempotency_keys" in tables()
    assert "password_hash" not in columns("users")

    # An evaluation that predates ownership: 0012 must backfill owner_id from evaluated_by.
    uid, sid, vid, eid = (str(uuid.uuid4()) for _ in range(4))
    sql("INSERT INTO users (id, email, name) VALUES (%s, 'old@example.com', 'Old')", (uid,))
    sql("INSERT INTO scorecards (id, name, owner_id) VALUES (%s, 'S', %s)", (sid, uid))
    sql(
        "INSERT INTO scorecard_versions (id, scorecard_id, version_number, created_by) VALUES (%s, %s, 1, %s)",
        (vid, sid, uid),
    )
    sql(
        "INSERT INTO evaluations (id, scorecard_version_id, name, evaluated_by) VALUES (%s, %s, 'E', %s)",
        (eid, vid, uid),
    )

    alembic("upgrade", "0012_auth_ownership")
    assert {"password_hash", "is_active", "failed_logins", "locked_until", "sessions_valid_after"} <= columns("users")
    assert {"ip", "user_agent", "request_id"} <= columns("audit_log")
    assert {"refresh_tokens", "password_reset_tokens", "oauth_identities"} <= tables()
    assert sql("SELECT owner_id::text FROM evaluations WHERE id=%s", (eid,)) == [(uid,)]
    # existing users stay passwordless but active
    assert sql("SELECT password_hash, is_active, failed_logins FROM users WHERE id=%s", (uid,)) == [(None, True, 0)]
    # owner_id is NOT NULL after the migration
    with pytest.raises(psycopg.errors.NotNullViolation):
        sql(
            "INSERT INTO evaluations (id, scorecard_version_id, name, evaluated_by) VALUES (%s, %s, 'E2', %s)",
            (str(uuid.uuid4()), vid, uid),
        )

    # Downgrade removes exactly what 0012 added, keeps the data, and can be re-applied.
    alembic("downgrade", "0011_scaling_leases")
    assert "owner_id" not in columns("evaluations") and "password_hash" not in columns("users")
    assert not ({"refresh_tokens", "password_reset_tokens", "oauth_identities"} & tables())
    assert "request_id" not in columns("audit_log")
    assert sql("SELECT count(*) FROM evaluations") == [(1,)]
    alembic("downgrade", "0010_ai_eval_pipeline")
    assert "idempotency_keys" not in tables() and "lease_owner" not in columns("evaluations")
    assert "turn_lease_owner" not in columns("chat_sessions")
    alembic("upgrade", "head")
    assert sql("SELECT owner_id::text FROM evaluations WHERE id=%s", (eid,)) == [(uid,)]


def test_upgrade_head_is_idempotent(scratch) -> None:
    alembic, _sql, _columns, _tables = scratch
    alembic("upgrade", "head")
    alembic("upgrade", "head")  # already at head: a no-op, not an error
