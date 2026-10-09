"""Authentication: signup, login, cookies, refresh rotation + reuse detection, logout, remember-me, lockout,
rate limiting, CSRF, forgot / reset password, e-mail verification, OAuth (not configured), production checks."""

from __future__ import annotations

import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import mailer
from app.auth import security as sec
from app.config import Settings, get_settings
from app.models.audit_log import AuditLog
from app.models.auth import RefreshToken
from app.models.user import User

PASSWORD = "correct-horse-battery-staple"
ANON = {"X-Test-Anonymous": "1"}


def make_user(db: Session, email: str = "alice@example.com", password: str | None = PASSWORD, **kw) -> User:
    user = User(
        email=email, name=kw.pop("name", "Alice"), password_hash=sec._hasher.hash(password) if password else None, **kw
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def login(client: TestClient, email="alice@example.com", password=PASSWORD, remember=False, **kw):
    headers = {**ANON, **kw.pop("headers", {})}
    return client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password, "remember_me": remember},
        headers=headers,
        **kw,
    )


def csrf_headers(client: TestClient) -> dict[str, str]:
    return {**ANON, "X-CSRF-Token": client.cookies.get("qs_csrf", "")}


def set_cookie_headers(resp) -> list[str]:
    return resp.headers.get_list("set-cookie")


@pytest.fixture()
def mails(monkeypatch):
    sent: list[tuple[str, str, str]] = []

    async def fake_send(to: str, subject: str, body: str) -> None:
        sent.append((to, subject, body))

    monkeypatch.setattr(mailer, "send_email", fake_send)
    return sent


def token_from(mails_, subject_part: str) -> str:
    body = next(b for (_to, s, b) in reversed(mails_) if subject_part in s)
    return re.search(r"token=([\w-]+)", body).group(1)


# --- signup -------------------------------------------------------------------------------------------


def test_signup_creates_an_account_and_signs_in(client: TestClient, db_session: Session, mails) -> None:
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": " New.User@Example.com ", "name": "New User", "password": PASSWORD},
        headers=ANON,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["email"] == "new.user@example.com" and body["user"]["email_verified"] is False
    assert "password" not in str(body).lower().replace("password_hash", "")
    me = client.get("/api/v1/me", headers=ANON)  # authenticated by the cookies alone
    assert me.status_code == 200 and me.json()["name"] == "New User"
    stored = db_session.execute(select(User).where(User.email == "new.user@example.com")).scalar_one()
    assert stored.password_hash.startswith("$argon2id$") and PASSWORD not in stored.password_hash
    assert mails and "Verify" in mails[0][1]


def test_signup_rejects_duplicates_and_weak_passwords(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    dup = client.post(
        "/api/v1/auth/signup", json={"email": "ALICE@example.com", "name": "x", "password": PASSWORD}, headers=ANON
    )
    assert dup.status_code == 409
    for weak in ("short", "password1234", "aaaaaaaaaaaaaaaa", "bob@example.com"):
        r = client.post(
            "/api/v1/auth/signup", json={"email": "bob@example.com", "name": "Bob", "password": weak}, headers=ANON
        )
        assert r.status_code == 422, weak


# --- login + cookies ----------------------------------------------------------------------------------


def test_login_sets_httponly_samesite_cookies_and_never_leaks_which_part_was_wrong(
    client: TestClient, db_session: Session
) -> None:
    make_user(db_session)
    ok = login(client)
    assert ok.status_code == 200 and ok.json()["token_type"] == "bearer" and ok.json()["expires_in"] == 900
    cookies = {c.split("=", 1)[0]: c for c in set_cookie_headers(ok)}
    assert {"qs_access", "qs_refresh", "qs_csrf"} <= cookies.keys()
    for name in ("qs_access", "qs_refresh"):
        assert "HttpOnly" in cookies[name] and "samesite=lax" in cookies[name].lower()
    assert "HttpOnly" not in cookies["qs_csrf"]  # readable by the page: double-submit token
    # the access token is a short-lived signed JWT with sub / exp / jti
    claims = jwt.decode(ok.json()["access_token"], get_settings().jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == str(db_session.execute(select(User.id)).scalar_one()) and claims["jti"]
    assert claims["exp"] - claims["iat"] == 900

    wrong_pw = login(client, password="wrong-password-123")
    unknown = login(client, email="nobody@example.com")
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()  # identical: no user enumeration


def test_remember_me_makes_the_refresh_cookie_persistent(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    short = next(c for c in set_cookie_headers(login(client)) if c.startswith("qs_refresh="))
    assert "max-age" not in short.lower()  # session cookie: gone when the browser closes
    client.cookies.clear()
    long = next(c for c in set_cookie_headers(login(client, remember=True)) if c.startswith("qs_refresh="))
    assert f"Max-Age={30 * 86400}" in long
    row = db_session.execute(select(RefreshToken).order_by(RefreshToken.created_at.desc())).scalars().first()
    assert row.remember is True and row.expires_at > datetime.now(UTC) + timedelta(days=29)


def test_expired_tampered_and_foreign_tokens_are_rejected(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    secret = get_settings().jwt_secret
    now = int(time.time())
    base = {"sub": str(user.id), "iat": now, "jti": "x", "typ": "access"}
    expired = jwt.encode({**base, "exp": now - 5}, secret, algorithm="HS256")
    wrong_key = jwt.encode({**base, "exp": now + 600}, "some-other-secret-value-0123456789abcdef", algorithm="HS256")
    none_alg = jwt.encode({**base, "exp": now + 600}, key=None, algorithm="none")
    legacy_uuid = str(user.id)  # the old dev stub accepted a raw user id as the bearer token
    for token in (expired, wrong_key, none_alg, legacy_uuid, "garbage"):
        r = client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {token}"})
        assert r.status_code == 401, token
    raw = TestClient(client.app)  # no test-helper translation: the server itself must ignore X-User-Id
    assert raw.get("/api/v1/me", headers={"X-User-Id": str(user.id)}).status_code == 401  # the old dev header is dead
    good = jwt.encode({**base, "exp": now + 600}, secret, algorithm="HS256")
    assert client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {good}"}).status_code == 200


def test_deactivated_user_loses_access_immediately(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    assert login(client).status_code == 200
    user.is_active = False
    db_session.commit()
    assert client.get("/api/v1/me", headers=ANON).status_code == 401
    client.cookies.clear()
    assert login(client).status_code == 401


# --- refresh rotation + reuse detection ----------------------------------------------------------------


def test_refresh_rotates_the_token_and_replay_revokes_the_whole_family(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    make_user(db_session)
    login(client)
    first = client.cookies.get("qs_refresh")

    r = client.post("/api/v1/auth/refresh", headers=csrf_headers(client))
    assert r.status_code == 200, r.text
    second = client.cookies.get("qs_refresh")
    assert second and second != first
    assert client.get("/api/v1/me", headers=ANON).status_code == 200

    # An attacker (or a stale tab) replays the first, already-rotated token.
    replay = TestClient(client.app)
    replay.cookies.set("qs_refresh", first)
    replay.cookies.set("qs_csrf", client.cookies.get("qs_csrf"))
    bad = replay.post("/api/v1/auth/refresh", headers=csrf_headers(replay))
    assert bad.status_code == 401
    # ...which burns the whole family: the legitimate, newest token no longer works either.
    again = client.post("/api/v1/auth/refresh", headers=csrf_headers(client))
    assert again.status_code == 401
    revoked = db_session.execute(select(RefreshToken)).scalars().all()
    assert revoked and all(t.revoked_at is not None for t in revoked)
    events = [a.diff.get("event") for a in db_session.execute(select(AuditLog)).scalars()]
    assert "refresh_reuse_detected" in events


def test_concurrent_refresh_within_the_grace_window_is_not_treated_as_theft(
    client: TestClient, db_session: Session
) -> None:
    make_user(db_session)
    login(client)
    first = client.cookies.get("qs_refresh")
    assert client.post("/api/v1/auth/refresh", headers=csrf_headers(client)).status_code == 200
    # A second tab still holding the old cookie refreshes a moment later: it gets an access token, no revocation.
    tab2 = TestClient(client.app)
    tab2.cookies.set("qs_refresh", first)
    tab2.cookies.set("qs_csrf", client.cookies.get("qs_csrf"))
    assert tab2.post("/api/v1/auth/refresh", headers=csrf_headers(tab2)).status_code == 200
    assert client.post("/api/v1/auth/refresh", headers=csrf_headers(client)).status_code == 200  # newest still valid


def test_refresh_without_cookie_or_csrf_is_rejected(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    assert client.post("/api/v1/auth/refresh", headers=ANON).status_code == 403  # no CSRF token
    login(client)
    assert client.post("/api/v1/auth/refresh", headers=ANON).status_code == 403
    client.cookies.delete("qs_refresh")
    assert client.post("/api/v1/auth/refresh", headers=csrf_headers(client)).status_code == 401


# --- logout --------------------------------------------------------------------------------------------


def test_logout_revokes_the_session_and_clears_cookies(client: TestClient, db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    make_user(db_session)
    login(client)
    refresh_cookie = client.cookies.get("qs_refresh")
    r = client.post("/api/v1/auth/logout", headers=csrf_headers(client))
    assert r.status_code == 200
    assert any(c.startswith("qs_access=") and "Max-Age=0" in c for c in set_cookie_headers(r))
    ghost = TestClient(client.app)
    ghost.cookies.set("qs_refresh", refresh_cookie)
    ghost.cookies.set("qs_csrf", "tok")
    assert ghost.post("/api/v1/auth/refresh", headers={**ANON, "X-CSRF-Token": "tok"}).status_code == 401
    events = [a.diff.get("event") for a in db_session.execute(select(AuditLog)).scalars()]
    assert "login" in events and "logout" in events


def test_logout_all_invalidates_every_access_token_immediately(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    token = login(client).json()["access_token"]
    other_device = TestClient(client.app)
    login(other_device)
    assert client.post("/api/v1/auth/logout-all", headers=csrf_headers(client)).status_code == 200
    for c in (client, other_device):
        c.cookies.clear()
    assert client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {token}"}).status_code == 401


# --- CSRF ----------------------------------------------------------------------------------------------


def test_cookie_authenticated_writes_need_the_csrf_token_and_a_friendly_origin(
    client: TestClient, db_session: Session
) -> None:
    make_user(db_session)
    login(client)
    body = {"name": "Renamed"}
    assert client.patch("/api/v1/me", json=body, headers=ANON).status_code == 403  # cookie, no header
    assert client.patch("/api/v1/me", json=body, headers={**ANON, "X-CSRF-Token": "forged"}).status_code == 403
    evil = {**csrf_headers(client), "Origin": "https://evil.example"}
    assert client.patch("/api/v1/me", json=body, headers=evil).status_code == 403  # right token, wrong site
    good = {**csrf_headers(client), "Origin": get_settings().frontend_url}
    assert client.patch("/api/v1/me", json=body, headers=good).status_code == 200
    assert client.get("/api/v1/me", headers=ANON).status_code == 200  # reads never need it


def test_bearer_token_requests_are_not_subject_to_csrf(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    token, _ = sec.create_access_token(user.id)
    r = client.patch("/api/v1/me", json={"name": "Scripted"}, headers={**ANON, "Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_login_from_a_foreign_origin_is_blocked(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    r = login(client, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# --- lockout + rate limits -----------------------------------------------------------------------------


def test_repeated_bad_passwords_lock_the_account_with_a_retry_after(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    for _ in range(get_settings().lockout_threshold):
        assert login(client, password="wrong-password-123").status_code == 401
    locked = login(client)  # even the RIGHT password is refused while locked
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
    db_session.refresh(user)
    assert user.locked_until is not None and user.failed_logins >= get_settings().lockout_threshold
    user.locked_until = datetime.now(UTC) - timedelta(seconds=1)  # the lock expires
    db_session.commit()
    ok = login(client)
    assert ok.status_code == 200
    db_session.refresh(user)
    assert user.failed_logins == 0 and user.locked_until is None


def test_login_endpoint_is_rate_limited_per_ip(client: TestClient, db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_login", "3/minute")
    statuses = [login(client, email=f"nobody{i}@example.com").status_code for i in range(5)]
    assert statuses[:3] == [401, 401, 401] and statuses[3:] == [429, 429]
    r = login(client, email="nobody9@example.com")
    assert "retry-after" in r.headers


def test_login_is_also_throttled_per_email_address(client: TestClient, db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_login", "100/minute")
    monkeypatch.setattr(get_settings(), "lockout_threshold", 1000)
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    results = [
        login(client, email="victim@example.com", password=f"guess-number-{i:03d}").status_code for i in range(105)
    ]
    assert 429 in results  # one address cannot be hammered even from rotating "IPs"


def test_expensive_routes_are_limited_per_user(client: TestClient, db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_export", "2/minute")
    a = make_user(db_session, "a@example.com")
    b = make_user(db_session, "b@example.com")
    ha = {**ANON, "Authorization": f"Bearer {sec.create_access_token(a.id)[0]}"}
    hb = {**ANON, "Authorization": f"Bearer {sec.create_access_token(b.id)[0]}"}
    body = {"evaluation_ids": [str(uuid.uuid4())]}
    codes_a = [client.post("/api/v1/evaluations/export", json=body, headers=ha).status_code for _ in range(3)]
    assert codes_a == [404, 404, 429]
    assert client.post("/api/v1/evaluations/export", json=body, headers=hb).status_code == 404  # B has its own budget


# --- forgot / reset / verify ---------------------------------------------------------------------------


def test_forgot_password_answers_the_same_for_known_and_unknown_addresses(
    client: TestClient, db_session: Session, mails
) -> None:
    make_user(db_session)
    known = client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"}, headers=ANON)
    unknown = client.post("/api/v1/auth/forgot-password", json={"email": "ghost@example.com"}, headers=ANON)
    assert known.status_code == unknown.status_code == 202 and known.json() == unknown.json()
    assert [m[0] for m in mails] == ["alice@example.com"]  # only the real account got mail


def test_reset_password_flow_is_single_use_and_ends_every_session(
    client: TestClient, db_session: Session, mails
) -> None:
    make_user(db_session)
    old_access = login(client).json()["access_token"]
    client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"}, headers=ANON)
    token = token_from(mails, "Reset")

    weak = client.post("/api/v1/auth/reset-password", json={"token": token, "password": "short"}, headers=ANON)
    assert weak.status_code == 422  # a rejected password does not spend the token
    new_pw = "a-brand-new-passphrase-42"
    ok = client.post("/api/v1/auth/reset-password", json={"token": token, "password": new_pw}, headers=ANON)
    assert ok.status_code == 200, ok.text
    again = client.post(
        "/api/v1/auth/reset-password", json={"token": token, "password": "another-passphrase-99"}, headers=ANON
    )
    assert again.status_code == 400  # single use

    assert client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {old_access}"}).status_code == 401
    assert login(client, password=PASSWORD).status_code == 401
    assert login(client, password=new_pw).status_code == 200


def test_expired_reset_token_is_refused(client: TestClient, db_session: Session, mails) -> None:
    from app.models.auth import PasswordResetToken

    make_user(db_session)
    client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"}, headers=ANON)
    token = token_from(mails, "Reset")
    row = db_session.execute(select(PasswordResetToken)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.commit()
    r = client.post(
        "/api/v1/auth/reset-password", json={"token": token, "password": "a-brand-new-passphrase-42"}, headers=ANON
    )
    assert r.status_code == 400


def test_a_passwordless_legacy_account_is_claimed_through_forgot_password(
    client: TestClient, db_session: Session, mails
) -> None:
    make_user(db_session, "designer@qualityscorecard.local", password=None, name="Designer")
    make_user(db_session, "legacy@example.com", password=None, name="Legacy")
    assert login(client, email="designer@qualityscorecard.local").status_code == 401
    assert (
        client.post(  # signup must not hand the legacy account to whoever types its address
            "/api/v1/auth/signup",
            json={"email": "legacy@example.com", "name": "x", "password": PASSWORD},
            headers=ANON,
        ).status_code
        == 409
    )
    client.post("/api/v1/auth/forgot-password", json={"email": "designer@qualityscorecard.local"}, headers=ANON)
    token = token_from(mails, "Reset")
    assert (
        client.post(
            "/api/v1/auth/reset-password", json={"token": token, "password": PASSWORD}, headers=ANON
        ).status_code
        == 200
    )
    assert login(client, email="designer@qualityscorecard.local").status_code == 200


def test_verify_email_token_is_single_use(client: TestClient, db_session: Session, mails) -> None:
    client.post("/api/v1/auth/signup", json={"email": "v@example.com", "name": "V", "password": PASSWORD}, headers=ANON)
    token = token_from(mails, "Verify")
    assert client.post("/api/v1/auth/verify-email", json={"token": token}, headers=ANON).status_code == 200
    assert client.post("/api/v1/auth/verify-email", json={"token": token}, headers=ANON).status_code == 400
    assert client.get("/api/v1/me", headers=ANON).json()["email_verified"] is True


# --- OAuth, headers, production safety -----------------------------------------------------------------


def test_oauth_providers_are_disabled_until_configured(client: TestClient) -> None:
    r = client.get("/api/v1/auth/providers", headers=ANON)
    assert r.status_code == 200
    assert {p["id"]: p["enabled"] for p in r.json()["providers"]} == {
        "google": False,
        "github": False,
        "microsoft": False,
    }
    for provider in ("google", "github", "microsoft"):
        start = client.get(f"/api/v1/auth/oauth/{provider}/start", headers=ANON, follow_redirects=False)
        assert start.status_code == 501 and "not configured" in start.json()["detail"]
    assert client.get("/api/v1/auth/oauth/nope/start", headers=ANON).status_code == 404


def test_oauth_start_builds_a_pkce_redirect_and_callback_rejects_a_bad_state(client: TestClient, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "oauth_google_client_id", "client-id")
    monkeypatch.setattr(s, "oauth_google_client_secret", "client-secret")
    start = client.get("/api/v1/auth/oauth/google/start", headers=ANON, follow_redirects=False)
    assert start.status_code == 302
    loc = start.headers["location"]
    assert loc.startswith("https://accounts.google.com/") and "code_challenge_method=S256" in loc and "state=" in loc
    assert any(c.startswith("qs_oauth=") and "HttpOnly" in c for c in set_cookie_headers(start))
    bad = client.get("/api/v1/auth/oauth/google/callback?code=abc&state=forged", headers=ANON, follow_redirects=False)
    assert bad.status_code == 303 and "error=oauth_state" in bad.headers["location"]


def test_security_headers_and_request_id(client: TestClient) -> None:
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert (
        r.headers["referrer-policy"] == "no-referrer"
        and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    )
    assert r.headers["x-request-id"]
    assert client.get("/health", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"] == "abc-123"
    # CORS: explicit origin allowed with credentials, unknown origin gets nothing, methods are explicit.
    origin = get_settings().cors_origin_list[0]
    ok = client.options("/api/v1/me", headers={"Origin": origin, "Access-Control-Request-Method": "PATCH"})
    assert (
        ok.headers["access-control-allow-origin"] == origin and ok.headers["access-control-allow-credentials"] == "true"
    )
    evil = client.options(
        "/api/v1/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )
    assert "access-control-allow-origin" not in evil.headers


def _prod(**overrides) -> Settings:
    """A fully valid production configuration built from random values (never committed secrets)."""
    base = {
        "environment": "production",
        "jwt_secret": secrets.token_urlsafe(48),
        "database_url": f"postgresql+psycopg://u:{secrets.token_urlsafe(24)}@db/x",
        "cors_origins": "https://app.corp.test",
        "frontend_url": "https://app.corp.test",
        "email_backend": "ses",
        "email_from": "no-reply@corp.test",
    }
    base.update(overrides)
    return Settings(**base)


def test_production_refuses_default_secrets_and_hides_docs() -> None:
    fine = _prod()
    fine.validate_production_settings()
    assert fine.cookies_secure is True and fine.docs_on is False
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        _prod(jwt_secret="").validate_production_settings()
    with pytest.raises(RuntimeError, match="EMAIL_BACKEND"):
        _prod(email_backend="log").validate_production_settings()
    dev = Settings(environment="development")
    dev.validate_production_settings()
    assert dev.cookies_secure is False and dev.docs_on is True


def test_interactive_docs_are_served_outside_production(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200


def test_argon2id_parameters_follow_the_owasp_profile(db_session: Session) -> None:
    user = make_user(db_session)
    assert user.password_hash.startswith("$argon2id$v=19$m=19456,t=2,p=1$")
