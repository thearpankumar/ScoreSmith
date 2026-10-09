"""First-admin flows: the one-time production bootstrap (`POST /auth/register-user` + BOOTSTRAP_TOKEN), the
non-production ADMIN_EMAIL / ADMIN_PASSWORD admin, admin-only user creation, role self-assignment, startup checks.

Every credential here is generated per run (`secrets`): the repo contains no usable secret."""

from __future__ import annotations

import asyncio
import logging
import secrets

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import bootstrap
from app.auth import security as sec
from app.config import Settings, get_settings
from app.models.audit_log import AuditLog
from app.models.user import User

ANON = {"X-Test-Anonymous": "1"}


def strong_password() -> str:
    return f"Aa1!{secrets.token_urlsafe(18)}"


def count_users(db: Session) -> int:
    db.expire_all()
    return db.execute(select(func.count()).select_from(User)).scalar_one()


@pytest.fixture()
def token(monkeypatch) -> str:
    value = secrets.token_urlsafe(40)
    monkeypatch.setattr(get_settings(), "bootstrap_token", SecretStr(value))
    return value


def register(client: TestClient, email: str, password: str, token: str | None = None, **extra):
    headers = dict(ANON)
    if token is not None:
        headers["X-Bootstrap-Token"] = token
    return client.post(
        "/api/v1/auth/register-user",
        json={"email": email, "name": "Admin One", "password": password, **extra},
        headers=headers,
    )


# --- production bootstrap -----------------------------------------------------------------------------


def test_bootstrap_creates_the_first_admin_then_closes_for_good(
    client: TestClient, db_session: Session, token: str
) -> None:
    assert client.get("/api/v1/auth/config", headers=ANON).json()["setup_required"] is True
    pw = strong_password()
    r = register(client, "  Root.Admin@Example.com ", pw, token)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["email"] == "root.admin@example.com" and body["role"] == "admin" and body["email_verified"] is True
    assert pw not in r.text
    user = db_session.execute(select(User)).scalar_one()
    assert user.password_hash and user.password_hash.startswith("$argon2id$")
    # The new admin can sign in.
    login = client.post(
        "/api/v1/auth/login", json={"email": "root.admin@example.com", "password": pw}, headers=ANON
    )
    assert login.status_code == 200
    client.cookies.clear()  # back to anonymous
    # Permanently closed: even the right token no longer works, and the setup flag flips.
    again = register(client, "second@example.com", strong_password(), token)
    assert again.status_code == 404
    assert count_users(db_session) == 1
    assert client.get("/api/v1/auth/config", headers=ANON).json()["setup_required"] is False
    events = [a.diff.get("event") for a in db_session.execute(select(AuditLog)).scalars()]
    assert "bootstrap_admin_created" in events


def test_bootstrap_rejects_missing_or_wrong_token_and_audits_it(
    client: TestClient, db_session: Session, token: str
) -> None:
    pw = strong_password()
    assert register(client, "a@example.com", pw).status_code == 403  # header missing
    assert register(client, "a@example.com", pw, "x" * len(token)).status_code == 403  # same length, wrong
    assert register(client, "a@example.com", pw, "").status_code == 403
    assert count_users(db_session) == 0
    events = [a.diff.get("event") for a in db_session.execute(select(AuditLog)).scalars()]
    assert events.count("bootstrap_token_rejected") == 3
    # The right token still works afterwards (failed attempts do not burn the flow).
    assert register(client, "a@example.com", pw, token).status_code == 201


def test_bootstrap_is_disabled_without_a_configured_token(client: TestClient, db_session: Session) -> None:
    assert get_settings().bootstrap_token.get_secret_value() == ""
    assert register(client, "a@example.com", strong_password(), "anything").status_code == 404
    assert client.get("/api/v1/auth/config", headers=ANON).json()["setup_required"] is False
    assert count_users(db_session) == 0


def test_bootstrap_enforces_the_admin_password_policy(client: TestClient, db_session: Session, token: str) -> None:
    for weak in ("short1!", "alllowercaseletters", "password12345", "Sh0rt!Pass"):
        r = register(client, "a@example.com", weak, token)
        assert r.status_code == 422, weak
    assert count_users(db_session) == 0


def test_bootstrap_is_rate_limited(client: TestClient, db_session: Session, token: str, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_register_user", "3/minute")
    codes = [register(client, "a@example.com", strong_password(), "wrong").status_code for _ in range(5)]
    assert codes[:3] == [403, 403, 403] and codes[3:] == [429, 429]


async def test_two_concurrent_bootstrap_calls_create_exactly_one_user(db_session: Session, token: str) -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:

        async def call(i: int) -> int:
            r = await ac.post(
                "/api/v1/auth/register-user",
                json={"email": f"admin{i}@example.com", "name": "A", "password": strong_password()},
                headers={"X-Bootstrap-Token": token},
            )
            return r.status_code

        codes = await asyncio.gather(*(call(i) for i in range(4)))
    assert sorted(codes) == [201, 404, 404, 404]
    assert count_users(db_session) == 1


# --- admin-created users + roles ----------------------------------------------------------------------


def make_admin_and_member(db: Session) -> tuple[User, User]:
    admin = User(
        email="admin@example.com", name="Admin", role="admin", password_hash=sec.hash_password_sync(strong_password())
    )
    member = User(email="member@example.com", name="Member", role="member")
    db.add_all([admin, member])
    db.commit()
    return admin, member


def test_only_an_admin_can_register_users_afterwards(client: TestClient, db_session: Session, token: str) -> None:
    admin, member = make_admin_and_member(db_session)
    body = {"email": "New@Example.com", "name": "New", "password": strong_password()}
    # anonymous (even with the bootstrap token) -> closed; member -> forbidden
    assert register(client, "x@example.com", strong_password(), token).status_code == 404
    as_member = client.post("/api/v1/auth/register-user", json=body, headers={"X-User-Id": str(member.id)})
    assert as_member.status_code == 403
    as_admin = client.post("/api/v1/auth/register-user", json=body, headers={"X-User-Id": str(admin.id)})
    assert as_admin.status_code == 201 and as_admin.json()["role"] == "member"
    assert as_admin.json()["email"] == "new@example.com"
    dup = client.post("/api/v1/auth/register-user", json=body, headers={"X-User-Id": str(admin.id)})
    assert dup.status_code == 409
    second_admin = client.post(
        "/api/v1/auth/register-user",
        json={**body, "email": "boss@example.com", "role": "admin"},
        headers={"X-User-Id": str(admin.id)},
    )
    assert second_admin.status_code == 201 and second_admin.json()["role"] == "admin"


def test_role_cannot_be_self_assigned(client: TestClient, db_session: Session) -> None:
    pw = strong_password()
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "evil@example.com", "name": "Evil", "password": pw, "role": "admin"},
        headers=ANON,
    )
    assert r.status_code == 422
    ok = client.post(
        "/api/v1/auth/signup", json={"email": "evil@example.com", "name": "Evil", "password": pw}, headers=ANON
    )
    assert ok.status_code == 201 and ok.json()["user"]["role"] == "member"
    bearer = {"Authorization": f"Bearer {ok.json()['access_token']}", **ANON}
    patch = client.patch("/api/v1/me", json={"name": "Evil", "role": "admin"}, headers=bearer)
    assert patch.status_code == 200 and patch.json()["role"] == "member"  # the field is ignored, never applied
    assert client.patch("/api/v1/me", json={"name": "Renamed"}, headers=bearer).json()["role"] == "member"
    db_session.expire_all()
    assert db_session.execute(select(User.role).where(User.email == "evil@example.com")).scalar_one() == "member"


def test_signup_can_be_switched_off(client: TestClient, db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "signup_enabled", False)
    assert client.get("/api/v1/auth/config", headers=ANON).json()["signup_enabled"] is False
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "a@example.com", "name": "A", "password": strong_password()},
        headers=ANON,
    )
    assert r.status_code == 403
    assert count_users(db_session) == 0


# --- non-production env admin -------------------------------------------------------------------------


@pytest.fixture()
def env_admin(monkeypatch) -> tuple[str, str]:
    email, pw = f"Boss-{secrets.token_hex(3)}@Example.com", strong_password()
    s = get_settings()
    monkeypatch.setattr(s, "admin_email", email)
    monkeypatch.setattr(s, "admin_password", SecretStr(pw))
    return email.lower(), pw


async def test_env_admin_is_created_once_even_when_replicas_race(
    db_session: Session, env_admin: tuple[str, str], caplog
) -> None:
    email, pw = env_admin
    with caplog.at_level(logging.DEBUG):
        results = await asyncio.gather(*(bootstrap.ensure_env_admin() for _ in range(5)))
    assert sorted(results) == ["created"] + ["exists"] * 4
    user = db_session.execute(select(User)).scalar_one()
    assert user.email == email and user.role == "admin" and user.email_verified_at is not None
    assert user.password_hash.startswith("$argon2id$")
    assert pw not in caplog.text and pw not in repr(get_settings())


async def test_env_admin_never_resets_an_existing_users_password(
    db_session: Session, env_admin: tuple[str, str]
) -> None:
    email, _pw = env_admin
    original = sec.hash_password_sync(strong_password())
    db_session.add(User(email=email, name="Existing", role="member", password_hash=original))
    db_session.commit()
    assert await bootstrap.ensure_env_admin() == "exists"
    db_session.expire_all()
    user = db_session.execute(select(User)).scalar_one()
    assert user.password_hash == original and user.role == "member"


async def test_dev_env_admin_may_use_a_short_password_and_log_in_by_username(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Non-production only: the env admin skips the strength policy and `admin` resolves to ADMIN_EMAIL."""
    s = get_settings()
    simple = "dev" + "pw"  # deliberately far below the 14-character admin policy
    monkeypatch.setattr(s, "admin_email", "root@dev.example")
    monkeypatch.setattr(s, "admin_password", SecretStr(simple))
    assert sec.admin_password_problem(simple, "root@dev.example")  # the policy would reject it...
    assert await bootstrap.ensure_env_admin() == "created"  # ...but the dev bootstrap does not apply it
    assert client.get("/api/v1/auth/config", headers=ANON).json()["username_login"] is True
    for identifier in ("admin", "ADMIN", " admin ", "root@dev.example"):
        r = client.post("/api/v1/auth/login", json={"email": identifier, "password": simple}, headers=ANON)
        assert r.status_code == 200, (identifier, r.text)
        assert r.json()["user"]["role"] == "admin"
        client.cookies.clear()
    bad = client.post("/api/v1/auth/login", json={"email": "admin", "password": "nope"}, headers=ANON)
    assert bad.status_code == 401
    other = client.post("/api/v1/auth/login", json={"email": "someone", "password": simple}, headers=ANON)
    assert other.status_code == 401


async def test_production_ignores_the_weak_admin_and_the_username_shortcut(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    s = get_settings()
    simple = "dev" + "pw"
    monkeypatch.setattr(s, "admin_email", "root@dev.example")
    monkeypatch.setattr(s, "admin_password", SecretStr(simple))
    monkeypatch.setattr(s, "environment", "production")
    assert await bootstrap.ensure_env_admin() == "disabled"
    assert count_users(db_session) == 0
    assert s.dev_username_login is False
    assert client.get("/api/v1/auth/config", headers=ANON).json()["username_login"] is False
    # Even with a real admin present, `admin` is just an unknown identifier in production.
    strong = strong_password()
    db_session.add(
        User(email="root@dev.example", name="Root", role="admin", password_hash=sec.hash_password_sync(strong))
    )
    db_session.commit()
    r = client.post("/api/v1/auth/login", json={"email": "admin", "password": strong}, headers=ANON)
    assert r.status_code == 401
    ok = client.post("/api/v1/auth/login", json={"email": "root@dev.example", "password": strong}, headers=ANON)
    assert ok.status_code == 200


def test_production_still_rejects_a_weak_password_for_the_bootstrap_admin(
    client: TestClient, db_session: Session, token: str, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "environment", "production")
    r = register(client, "root@corp.example", "dev" + "pw", token)
    assert r.status_code in (400, 422), r.text
    assert count_users(db_session) == 0


def test_set_password_allow_weak_is_refused_in_production(monkeypatch, capsys) -> None:
    from app.scripts import set_password

    monkeypatch.setattr(get_settings(), "environment", "production")
    assert set_password.main(["x@example.com", "--allow-weak", "--password-stdin"]) == 1
    assert "refused when ENV=production" in capsys.readouterr().err


async def test_production_ignores_env_admin_credentials(
    db_session: Session, env_admin: tuple[str, str], monkeypatch, caplog
) -> None:
    monkeypatch.setattr(get_settings(), "environment", "production")
    assert await bootstrap.ensure_env_admin() == "disabled"
    assert count_users(db_session) == 0
    # ...and the startup validation says so loudly.
    prod = Settings(
        environment="production",
        jwt_secret=secrets.token_urlsafe(48),
        database_url=f"postgresql+psycopg://u:{secrets.token_urlsafe(24)}@db/x",
        cors_origins="https://app.corp.test",
        frontend_url="https://app.corp.test",
        email_backend="ses",
        admin_email="x@corp.test",
        admin_password=SecretStr(strong_password()),
    )
    logging.getLogger("app.config").disabled = False  # alembic's fileConfig() disables pre-existing loggers
    with caplog.at_level(logging.WARNING):
        prod.validate_production_settings()
    assert "IGNORED in production" in caplog.text


# --- startup validation -------------------------------------------------------------------------------


def _prod_kwargs(**over) -> dict:
    base = {
        "environment": "production",
        "jwt_secret": secrets.token_urlsafe(48),
        "database_url": f"postgresql+psycopg://u:{secrets.token_urlsafe(24)}@db/x",
        "cors_origins": "https://app.corp.test",
        "frontend_url": "https://app.corp.test",
        "email_backend": "ses",
    }
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"jwt_secret": ""}, "JWT_SECRET"),
        ({"jwt_secret": "short"}, "JWT_SECRET"),
        ({"jwt_secret": "change_me_" + "x" * 40}, "JWT_SECRET"),
        ({"database_url": ""}, "DATABASE_URL"),
        ({"database_url": "postgresql+psycopg://u@db/x"}, "database password"),
        ({"database_url": f"postgresql+psycopg://u:{'short'}@db/x"}, "database password"),
        ({"database_url": "postgresql+psycopg://u:{}@db/x".format("change_me_please_ok")}, "database password"),
        ({"cookie_secure": False}, "COOKIE_SECURE"),
        ({"docs_enabled": True}, "DOCS_ENABLED"),
        ({"cors_origins": "*"}, "CORS_ORIGINS"),
        ({"cors_origins": "http://localhost:3000"}, "CORS_ORIGINS"),
        ({"frontend_url": "http://localhost:3000"}, "FRONTEND_URL"),
        ({"bootstrap_token": SecretStr("tooshort")}, "BOOTSTRAP_TOKEN"),
    ],
)
def test_production_refuses_to_start_with_unsafe_settings(override: dict, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        Settings(**_prod_kwargs(**override)).validate_production_settings()


def test_production_requires_cors_and_frontend_url_to_be_set_explicitly() -> None:
    kw = _prod_kwargs()
    del kw["cors_origins"]
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        Settings(**kw).validate_production_settings()
    kw = _prod_kwargs()
    del kw["frontend_url"]
    with pytest.raises(RuntimeError, match="FRONTEND_URL"):
        Settings(**kw).validate_production_settings()


def test_env_var_switches_production_on_and_signup_defaults_follow(monkeypatch) -> None:
    monkeypatch.setenv("ENV", "production")
    prod = Settings()
    assert prod.is_production and prod.signup_allowed is False
    monkeypatch.setenv("SIGNUP_ENABLED", "true")
    assert Settings().signup_allowed is True
    monkeypatch.delenv("ENV")
    monkeypatch.delenv("SIGNUP_ENABLED")
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert Settings().is_production
    monkeypatch.delenv("ENVIRONMENT")
    dev = Settings()
    assert not dev.is_production and dev.signup_allowed is True


def test_development_without_a_jwt_secret_gets_a_random_one_and_a_warning(monkeypatch, caplog) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    logging.getLogger("app.config").disabled = False  # alembic's fileConfig() disables pre-existing loggers
    with caplog.at_level(logging.WARNING):
        a, b = Settings(), Settings()
    assert len(a.jwt_secret) >= 32 and a.jwt_secret != b.jwt_secret
    assert "EPHEMERAL" in caplog.text
