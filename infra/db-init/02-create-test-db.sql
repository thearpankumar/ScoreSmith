-- Creates a dedicated database for the backend test suite (backend/tests/conftest.py),
-- on the SAME Postgres service/instance as the dev/demo database — Postgres supports
-- multiple databases per instance, so this avoids a second `db` service in
-- docker-compose.yml. Only runs on first container initialization (an empty data
-- volume), per Postgres's own docker-entrypoint-initdb.d semantics — see backend/README.md
-- and infra/.env.example (TEST_DATABASE_URL) for what to do on an already-initialized
-- instance (a one-off `CREATE DATABASE quality_scorecard_test OWNER qs_app;`).
--
-- Same extensions as the main DB (see app/models/*.py's use of pgvector/ltree types) so
-- Alembic migrations succeed against this database too. No explicit OWNER clause: this
-- script always runs connected as $POSTGRES_USER (the postgres image's own
-- docker-entrypoint-initdb.d convention), which is who CREATE DATABASE defaults the
-- owner to anyway.
SELECT 'CREATE DATABASE quality_scorecard_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'quality_scorecard_test')
\gexec

\connect quality_scorecard_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS ltree;
