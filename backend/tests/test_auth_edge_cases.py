# ruff: noqa: F811  (pytest fixtures imported from sibling test modules are re-declared as test arguments)
"""Auth edge cases beyond tests/test_auth.py: token-type confusion, session invalidation boundaries, lockout
backoff arithmetic, refresh/CSRF/logout corner cases, signup + profile validation, e-mail token abuse,
admin-created accounts (role matrix) and the open-redirect-free `/auth/config` + health surface."""

from __future__ import annotations

import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import security as sec
from app.config import get_settings
from app.models.audit_log import AuditLog
from app.models.auth import PasswordResetToken, RefreshToken
from app.models.user import User
from tests.test_auth import (  # noqa: F401
    ANON,
    PASSWORD,
    csrf_headers,
    login,
    mails,
    make_user,
    set_cookie_headers,
    token_from,
)


def bearer(user: User) -> dict[str, str]:
    return {**ANON, "Authorization": f"Bearer {sec.create_access_token(user.id)[0]}"}


def strong() -> str:
    return f"Aa1!{secrets.token_urlsafe(18)}"


# --- token confusion / session validity ---------------------------------------------------------------


def test_refresh_token_or_wrong_typ_jwt_is_not_an_access_token(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    login(client)
    raw_refresh = client.cookies.get("qs_refresh")
    now = int(time.time())
    base = {"sub": str(user.id), "iat": now, "exp": now + 600, "jti": "j"}
    wrong_typ = jwt.encode({**base, "typ": "refresh"}, get_settings().jwt_secret, algorithm="HS256")
    for token in (raw_refresh, wrong_typ):
        r = client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {token}"})
        assert r.status_code == 401
    # ...and the refresh cookie used as the access cookie gets nowhere either
    other = TestClient(client.app)
    other.cookies.set("qs_access", raw_refresh)
    assert other.get("/api/v1/me", headers=ANON).status_code == 401


def test_token_for_unknown_user_or_garbage_subject_is_401(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    ghost = sec.create_access_token(uuid.uuid4())[0]
    assert client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {ghost}"}).status_code == 401
    now = int(time.time())
    bad_sub = jwt.encode(
        {"sub": "not-a-uuid", "iat": now, "exp": now + 60, "jti": "j", "typ": "access"},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    assert client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {bad_sub}"}).status_code == 401


def test_401_responses_carry_www_authenticate_and_never_leak_the_reason(client: TestClient) -> None:
    missing = client.get("/api/v1/me", headers=ANON)
    garbage = client.get("/api/v1/me", headers={**ANON, "Authorization": "Bearer nope"})
    assert missing.status_code == garbage.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer" and garbage.headers["www-authenticate"] == "Bearer"
    assert "jwt" not in garbage.text.lower() and "signature" not in garbage.text.lower()


def test_sessions_valid_after_cuts_off_older_tokens_but_not_newer_ones(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    old = sec.create_access_token(user.id)[0]
    time.sleep(0.01)
    user.sessions_valid_after = datetime.now(UTC)
    db_session.commit()
    time.sleep(0.01)
    new = sec.create_access_token(user.id)[0]
    assert client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {old}"}).status_code == 401
    assert client.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {new}"}).status_code == 200


def test_inactive_user_with_the_right_password_gets_the_same_401_and_no_session(
    client: TestClient, db_session: Session
) -> None:
    make_user(db_session, "gone@example.com", is_active=False)
    make_user(db_session, "here@example.com")
    gone = login(client, email="gone@example.com")
    nope = login(client, email="here@example.com", password="wrong-password-123")
    assert gone.status_code == 401 and gone.json() == nope.json()
    assert not set_cookie_headers(gone)
    assert db_session.execute(select(RefreshToken)).first() is None


def test_email_is_case_and_whitespace_insensitive_at_login(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "mixed@example.com")
    assert login(client, email="  MiXeD@Example.COM ").status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": "a@example.com"},
        {"password": "x"},
        {"email": "ab", "password": "x"},  # shorter than 3
        {"email": "a@example.com", "password": ""},
        {"email": "a@example.com", "password": "x" * 1025},
        {"email": "a" * 321, "password": "x"},
        {"email": "a@example.com", "password": ["x"]},
    ],
)
def test_login_validation_errors_are_422(client: TestClient, payload: dict) -> None:
    assert client.post("/api/v1/auth/login", json=payload, headers=ANON).status_code == 422


def test_lockout_backoff_doubles_up_to_the_cap(client: TestClient, db_session: Session, monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "lockout_threshold", 2)
    monkeypatch.setattr(s, "lockout_base_minutes", 5)
    monkeypatch.setattr(s, "lockout_max_minutes", 12)
    monkeypatch.setattr(s, "rate_limit_login", "1000/minute")
    user = make_user(db_session)
    expected = {2: 5, 3: 10, 4: 12, 5: 12}  # failures -> minutes (5, 10, then capped at 12)
    for failures in range(1, 6):
        user.locked_until = None  # let the next wrong attempt through
        db_session.commit()
        assert login(client, password="wrong-password-123").status_code == 401
        db_session.refresh(user)
        if failures in expected:
            minutes = (user.locked_until - datetime.now(UTC)).total_seconds() / 60
            assert expected[failures] - 0.2 < minutes <= expected[failures], (failures, minutes)
        else:
            assert user.locked_until is None
    assert user.failed_logins == 5


def test_unknown_email_never_creates_lockout_state_or_audit_rows(client: TestClient, db_session: Session) -> None:
    for _ in range(8):
        assert login(client, email="ghost@example.com", password="wrong-password-123").status_code == 401
    assert db_session.execute(select(AuditLog)).first() is None


def test_successful_login_after_failures_resets_the_counter(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    for _ in range(get_settings().lockout_threshold - 1):
        login(client, password="wrong-password-123")
    assert login(client).status_code == 200
    db_session.refresh(user)
    assert user.failed_logins == 0 and user.locked_until is None


def test_login_upgrades_a_weak_password_hash(client: TestClient, db_session: Session) -> None:
    from argon2 import PasswordHasher

    user = make_user(db_session, password=None)
    user.password_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
    db_session.commit()
    old = user.password_hash
    assert login(client).status_code == 200
    db_session.refresh(user)
    assert user.password_hash != old and not sec.needs_rehash(user.password_hash)


# --- refresh: expiry, inactive, isolation between devices, remember carry-over ---------------------------


def test_expired_refresh_token_is_refused_and_the_cookies_are_cleared(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    login(client)
    row = db_session.execute(select(RefreshToken)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    r = client.post("/api/v1/auth/refresh", headers=csrf_headers(client))
    assert r.status_code == 401
    cleared = {c.split("=", 1)[0] for c in set_cookie_headers(r) if "Max-Age=0" in c}
    assert {"qs_access", "qs_refresh", "qs_csrf"} <= cleared


def test_refresh_for_a_deactivated_user_is_401(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    login(client)
    user.is_active = False
    db_session.commit()
    assert client.post("/api/v1/auth/refresh", headers=csrf_headers(client)).status_code == 401


def test_unknown_refresh_token_is_401(client: TestClient, db_session: Session) -> None:
    client.cookies.set("qs_refresh", secrets.token_urlsafe(48))
    client.cookies.set("qs_csrf", "tok")
    assert client.post("/api/v1/auth/refresh", headers={**ANON, "X-CSRF-Token": "tok"}).status_code == 401


def test_reuse_of_one_device_family_leaves_other_devices_signed_in(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)
    make_user(db_session)
    laptop, phone = client, TestClient(client.app)
    login(laptop)
    login(phone)
    stolen = laptop.cookies.get("qs_refresh")
    assert laptop.post("/api/v1/auth/refresh", headers=csrf_headers(laptop)).status_code == 200
    thief = TestClient(client.app)
    thief.cookies.set("qs_refresh", stolen)
    thief.cookies.set("qs_csrf", "tok")
    assert thief.post("/api/v1/auth/refresh", headers={**ANON, "X-CSRF-Token": "tok"}).status_code == 401
    assert laptop.post("/api/v1/auth/refresh", headers=csrf_headers(laptop)).status_code == 401  # family burned
    assert phone.post("/api/v1/auth/refresh", headers=csrf_headers(phone)).status_code == 200  # other family fine


def test_refresh_preserves_remember_me_and_the_csrf_token(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    login(client, remember=True)
    csrf_before = client.cookies.get("qs_csrf")
    r = client.post("/api/v1/auth/refresh", headers=csrf_headers(client))
    assert r.status_code == 200
    refresh_cookie = next(c for c in set_cookie_headers(r) if c.startswith("qs_refresh="))
    assert f"Max-Age={get_settings().refresh_days_remember * 86400}" in refresh_cookie
    assert client.cookies.get("qs_csrf") == csrf_before  # the page's CSRF token survives a rotation
    rows = db_session.execute(select(RefreshToken).order_by(RefreshToken.created_at)).scalars().all()
    assert len(rows) == 2 and all(r.remember for r in rows) and rows[0].family_id == rows[1].family_id
    assert rows[0].used_at is not None and rows[1].used_at is None


def test_session_login_refresh_stays_a_session_cookie(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    login(client, remember=False)
    r = client.post("/api/v1/auth/refresh", headers=csrf_headers(client))
    refresh_cookie = next(c for c in set_cookie_headers(r) if c.startswith("qs_refresh="))
    assert "max-age" not in refresh_cookie.lower()
    row = db_session.execute(select(RefreshToken).where(RefreshToken.used_at.is_(None))).scalar_one()
    assert row.expires_at <= datetime.now(UTC) + timedelta(hours=get_settings().refresh_hours_session, minutes=1)


def test_refresh_is_rate_limited(client: TestClient, db_session: Session, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_refresh", "2/minute")
    codes = [client.post("/api/v1/auth/refresh", headers={**ANON, "X-CSRF-Token": "x"}).status_code for _ in range(4)]
    assert codes[:2] == [403, 403] and codes[2:] == [429, 429]


# --- logout corner cases ------------------------------------------------------------------------------


def test_logout_without_any_session_is_a_harmless_200(client: TestClient) -> None:
    assert client.post("/api/v1/auth/logout", headers=ANON).status_code == 200


def test_logout_with_cookies_but_no_csrf_header_is_403_and_keeps_the_session(
    client: TestClient, db_session: Session
) -> None:
    make_user(db_session)
    login(client)
    assert client.post("/api/v1/auth/logout", headers=ANON).status_code == 403
    assert client.get("/api/v1/me", headers=ANON).status_code == 200
    assert db_session.execute(select(RefreshToken).where(RefreshToken.revoked_at.is_not(None))).first() is None


def test_logout_from_a_foreign_origin_is_blocked_even_without_cookies(client: TestClient) -> None:
    assert client.post("/api/v1/auth/logout", headers={**ANON, "Origin": "https://evil.example"}).status_code == 403


def test_logout_all_requires_authentication_and_revokes_every_device(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    assert client.post("/api/v1/auth/logout-all", headers=ANON).status_code == 401
    login(client)
    login(TestClient(client.app))
    assert client.post("/api/v1/auth/logout-all", headers=csrf_headers(client)).status_code == 200
    assert all(t.revoked_at is not None for t in db_session.execute(select(RefreshToken)).scalars())


# --- CSRF across the API --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/scorecards", {"name": "Csrf"}),
        ("PATCH", "/api/v1/me", {"name": "Csrf"}),
        ("POST", "/api/v1/chat/sessions", {"message": "hi"}),
        ("DELETE", f"/api/v1/scorecards/{uuid.uuid4()}", None),
        ("POST", "/api/v1/evaluations/export", {"evaluation_ids": [str(uuid.uuid4())]}),
        ("POST", "/api/v1/auth/logout-all", None),
    ],
)
def test_every_unsafe_method_on_cookie_auth_needs_csrf(
    client: TestClient, db_session: Session, method: str, path: str, body: dict | None
) -> None:
    make_user(db_session)
    login(client)
    assert client.request(method, path, json=body, headers=ANON).status_code == 403
    assert client.request(method, path, json=body, headers={**ANON, "X-CSRF-Token": "forged"}).status_code == 403
    # with the right token the request is no longer rejected for CSRF (it may legitimately 404/422 afterwards)
    assert client.request(method, path, json=body, headers=csrf_headers(client)).status_code != 403


def test_safe_methods_never_need_csrf_and_a_bearer_header_overrides_the_cookie(
    client: TestClient, db_session: Session
) -> None:
    user = make_user(db_session)
    login(client)
    assert client.get("/api/v1/scorecards", headers=ANON).status_code == 200
    # Bearer present next to cookies: the request authenticates by header, so no CSRF header is needed.
    assert client.patch("/api/v1/me", json={"name": "Via bearer"}, headers=bearer(user)).status_code == 200


# --- signup + profile validation ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "a@example.com", "name": "A", "password": PASSWORD, "role": "admin"},  # no role escalation
        {"email": "a@example.com", "name": "A", "password": PASSWORD, "is_active": True},
        {"email": "a@example.com", "name": "A", "password": PASSWORD, "email_verified_at": "2020-01-01T00:00:00Z"},
        {"email": "not-an-email", "name": "A", "password": PASSWORD},
        {"email": "a@example.com", "name": "", "password": PASSWORD},
        {"email": "a@example.com", "name": "x" * 201, "password": PASSWORD},
        {"email": "a@example.com", "name": "A", "password": ""},
        {"email": "a@example.com", "name": "A", "password": "x" * 1025},
        {"email": "a@example.com", "password": PASSWORD},
        {},
    ],
)
def test_signup_rejects_bad_payloads_without_creating_a_user(
    client: TestClient, db_session: Session, payload: dict
) -> None:
    assert client.post("/api/v1/auth/signup", json=payload, headers=ANON).status_code == 422
    assert db_session.execute(select(User)).first() is None


def test_signup_name_boundaries_and_trimming(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    ok = client.post(
        "/api/v1/auth/signup",
        json={"email": "edge@example.com", "name": "  " + "n" * 196 + "  ", "password": PASSWORD},
        headers=ANON,
    )
    assert ok.status_code == 201 and ok.json()["user"]["name"] == "n" * 196


def test_signup_with_a_blank_name_is_rejected(client: TestClient, db_session: Session) -> None:
    r = client.post(
        "/api/v1/auth/signup", json={"email": "blank@example.com", "name": "   ", "password": PASSWORD}, headers=ANON
    )
    assert r.status_code == 422
    assert db_session.execute(select(User)).first() is None


def test_signup_password_length_boundaries(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    s = get_settings()
    base = "aB3$xY7!qZ"

    def attempt(n: int, i: int):
        return client.post(
            "/api/v1/auth/signup",
            json={"email": f"len{i}@example.com", "name": "L", "password": (base * 20)[:n]},
            headers=ANON,
        )

    assert attempt(s.password_min_length - 1, 1).status_code == 422
    assert attempt(s.password_min_length, 2).status_code == 201
    client.cookies.clear()
    assert attempt(s.password_max_length, 3).status_code == 201
    client.cookies.clear()
    assert attempt(s.password_max_length + 1, 4).status_code == 422


def test_signup_from_a_foreign_origin_is_blocked_and_creates_nothing(client: TestClient, db_session: Session) -> None:
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "x@example.com", "name": "X", "password": PASSWORD},
        headers={**ANON, "Origin": "https://evil.example"},
    )
    assert r.status_code == 403 and db_session.execute(select(User)).first() is None


def test_signup_is_rate_limited_per_ip(client: TestClient, db_session: Session, monkeypatch, mails) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "rate_limit_signup", "2/minute")
    codes = [
        client.post(
            "/api/v1/auth/signup", json={"email": f"s{i}@example.com", "name": "S", "password": PASSWORD}, headers=ANON
        ).status_code
        for i in range(4)
    ]
    assert codes == [201, 201, 429, 429]


def test_signup_disabled_blocks_signup_but_not_login_and_is_advertised(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    make_user(db_session)
    monkeypatch.setattr(get_settings(), "signup_enabled", False)
    r = client.post(
        "/api/v1/auth/signup", json={"email": "n@example.com", "name": "N", "password": PASSWORD}, headers=ANON
    )
    assert r.status_code == 403 and "disabled" in r.json()["detail"].lower()
    assert client.get("/api/v1/auth/config", headers=ANON).json()["signup_enabled"] is False
    assert login(client).status_code == 200


def test_signup_email_duplicate_check_is_case_insensitive_and_audited_signup(
    client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
) -> None:
    ok = client.post(
        "/api/v1/auth/signup", json={"email": "Dup@Example.com", "name": "D", "password": PASSWORD}, headers=ANON
    )
    assert ok.status_code == 201
    client.cookies.clear()
    dup = client.post(
        "/api/v1/auth/signup", json={"email": "dup@EXAMPLE.com", "name": "D", "password": PASSWORD}, headers=ANON
    )
    assert dup.status_code == 409
    assert db_session.execute(select(AuditLog).where(AuditLog.diff["event"].astext == "signup")).scalars().all()
    assert len(db_session.execute(select(User)).scalars().all()) == 1


def test_signup_new_user_is_a_member_with_unverified_email(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    r = client.post(
        "/api/v1/auth/signup", json={"email": "m@example.com", "name": "M", "password": PASSWORD}, headers=ANON
    )
    assert r.json()["user"]["role"] == "member" and r.json()["user"]["email_verified"] is False
    assert db_session.execute(select(User)).scalar_one().role == "member"


# --- /me ----------------------------------------------------------------------------------------------


def test_me_patch_validates_and_ignores_privileged_fields(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    h = bearer(user)
    for bad in ({"name": ""}, {"name": "x" * 201}, {}, {"name": None}):
        assert client.patch("/api/v1/me", json=bad, headers=h).status_code == 422
    r = client.patch(
        "/api/v1/me",
        json={"name": " Newname ", "role": "admin", "email": "hacker@example.com", "is_active": False},
        headers=h,
    )
    assert r.status_code == 200 and r.json()["name"] == "Newname" and r.json()["role"] == "member"
    db_session.refresh(user)
    assert user.role == "member" and user.email == "alice@example.com" and user.is_active is True
    assert client.get("/api/v1/me", headers=h).json() == client.get("/api/v1/auth/me", headers=h).json()


def test_me_patch_with_a_blank_name_is_rejected(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    assert client.patch("/api/v1/me", json={"name": "    "}, headers=bearer(user)).status_code == 422
    db_session.refresh(user)
    assert user.name == "Alice"


def test_auth_responses_are_not_cacheable(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    assert client.get("/api/v1/me", headers=bearer(user)).headers["cache-control"] == "no-store"
    assert login(client).headers["cache-control"] == "no-store"
    assert client.get("/api/v1/auth/config", headers=ANON).headers["cache-control"] == "no-store"


def test_me_response_never_contains_credentials(client: TestClient, db_session: Session) -> None:
    user = make_user(db_session)
    body = client.get("/api/v1/me", headers=bearer(user)).json()
    assert set(body) == {"id", "email", "name", "role", "email_verified", "created_at"}


# --- forgot / reset / verify abuse ---------------------------------------------------------------------


def test_forgot_password_inactive_account_gets_no_mail_but_the_same_answer(
    client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
) -> None:
    make_user(db_session, "off@example.com", is_active=False)
    r = client.post("/api/v1/auth/forgot-password", json={"email": "off@example.com"}, headers=ANON)
    assert r.status_code == 202 and not mails
    assert db_session.execute(select(PasswordResetToken)).first() is None


def test_forgot_password_is_capped_per_address_without_revealing_it(
    client: TestClient,
    db_session: Session,
    mails,
    monkeypatch,  # noqa: F811
) -> None:
    make_user(db_session)
    monkeypatch.setattr(get_settings(), "rate_limit_forgot", "100/hour")
    for _ in range(6):
        r = client.post("/api/v1/auth/forgot-password", json={"email": "ALICE@example.com"}, headers=ANON)
        assert r.status_code == 202
    assert len(mails) == 3  # per-address ceiling of 3/hour; the extra requests looked identical to the caller


def test_forgot_password_is_rate_limited_per_ip(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_forgot", "2/hour")
    codes = [
        client.post("/api/v1/auth/forgot-password", json={"email": f"g{i}@example.com"}, headers=ANON).status_code
        for i in range(3)
    ]
    assert codes == [202, 202, 429]


def test_forgot_password_validation_and_foreign_origin(client: TestClient) -> None:
    assert client.post("/api/v1/auth/forgot-password", json={"email": "x"}, headers=ANON).status_code == 422
    assert client.post("/api/v1/auth/forgot-password", json={}, headers=ANON).status_code == 422
    evil = {**ANON, "Origin": "https://evil.example"}
    assert client.post("/api/v1/auth/forgot-password", json={"email": "a@example.com"}, headers=evil).status_code == 403


def test_reset_token_validation_and_purpose_separation(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    client.post("/api/v1/auth/signup", json={"email": "p@example.com", "name": "P", "password": PASSWORD}, headers=ANON)
    verify_token = token_from(mails, "Verify")
    new_pw = "a-brand-new-passphrase-42"
    # a verify token cannot reset a password, and a reset token cannot verify an e-mail
    assert (
        client.post(
            "/api/v1/auth/reset-password", json={"token": verify_token, "password": new_pw}, headers=ANON
        ).status_code
        == 400
    )
    client.post("/api/v1/auth/forgot-password", json={"email": "p@example.com"}, headers=ANON)
    reset_token = token_from(mails, "Reset")
    assert client.post("/api/v1/auth/verify-email", json={"token": reset_token}, headers=ANON).status_code == 400
    # the failed cross-use did not burn either token
    assert client.post("/api/v1/auth/verify-email", json={"token": verify_token}, headers=ANON).status_code == 200
    ok = client.post("/api/v1/auth/reset-password", json={"token": reset_token, "password": new_pw}, headers=ANON)
    assert ok.status_code == 200
    # length boundaries of the token field itself
    for bad in ("short", "x" * 201):
        assert (
            client.post(
                "/api/v1/auth/reset-password", json={"token": bad, "password": new_pw}, headers=ANON
            ).status_code
            == 422
        )
        assert client.post("/api/v1/auth/verify-email", json={"token": bad}, headers=ANON).status_code == 422
    assert (
        client.post(
            "/api/v1/auth/reset-password", json={"token": secrets.token_urlsafe(40), "password": new_pw}, headers=ANON
        ).status_code
        == 400
    )


def test_reset_clears_lockout_verifies_email_and_rejects_the_old_password_as_new(
    client: TestClient,
    db_session: Session,
    mails,  # noqa: F811
) -> None:
    user = make_user(db_session)
    user.failed_logins, user.locked_until = 9, datetime.now(UTC) + timedelta(minutes=30)
    db_session.commit()
    client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"}, headers=ANON)
    token = token_from(mails, "Reset")
    # a password equal to the e-mail local part is refused and does not spend the token
    r = client.post("/api/v1/auth/reset-password", json={"token": token, "password": "alice@example.com"}, headers=ANON)
    assert r.status_code == 422
    ok = client.post(
        "/api/v1/auth/reset-password", json={"token": token, "password": "fresh-passphrase-2026"}, headers=ANON
    )
    assert ok.status_code == 200
    db_session.refresh(user)
    assert user.failed_logins == 0 and user.locked_until is None and user.email_verified_at is not None
    assert login(client, password="fresh-passphrase-2026").status_code == 200


def test_reset_for_a_deactivated_account_is_refused(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    user = make_user(db_session)
    client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"}, headers=ANON)
    token = token_from(mails, "Reset")
    user.is_active = False
    db_session.commit()
    r = client.post(
        "/api/v1/auth/reset-password", json={"token": token, "password": "fresh-passphrase-2026"}, headers=ANON
    )
    assert r.status_code == 400


def test_expired_verify_token_is_refused(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    client.post("/api/v1/auth/signup", json={"email": "e@example.com", "name": "E", "password": PASSWORD}, headers=ANON)
    token = token_from(mails, "Verify")
    row = db_session.execute(select(PasswordResetToken)).scalar_one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    assert client.post("/api/v1/auth/verify-email", json={"token": token}, headers=ANON).status_code == 400


def test_tokens_are_stored_hashed_only(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    client.post("/api/v1/auth/signup", json={"email": "h@example.com", "name": "H", "password": PASSWORD}, headers=ANON)
    token = token_from(mails, "Verify")
    refresh = client.cookies.get("qs_refresh")
    stored = {r.token_hash for r in db_session.execute(select(PasswordResetToken)).scalars()}
    stored |= {r.token_hash for r in db_session.execute(select(RefreshToken)).scalars()}
    assert sec.hash_token(token) in stored and sec.hash_token(refresh) in stored
    assert token not in stored and refresh not in stored


def test_resend_verification_rules(client: TestClient, db_session: Session, mails) -> None:  # noqa: F811
    assert client.post("/api/v1/auth/resend-verification", headers=ANON).status_code == 401
    client.post("/api/v1/auth/signup", json={"email": "r@example.com", "name": "R", "password": PASSWORD}, headers=ANON)
    before = len(mails)
    for _ in range(5):
        assert client.post("/api/v1/auth/resend-verification", headers=csrf_headers(client)).status_code == 202
    # signup already sent #1 and at most 3 per hour are issued in total
    assert len(mails) - before == 2
    token = token_from(mails, "Verify")
    client.post("/api/v1/auth/verify-email", json={"token": token}, headers=ANON)
    sent = len(mails)
    assert client.post("/api/v1/auth/resend-verification", headers=csrf_headers(client)).status_code == 202
    assert len(mails) == sent  # verified accounts get nothing


# --- admin-created accounts: role matrix -----------------------------------------------------------------


def _admin_and_member(db: Session) -> tuple[User, User]:
    admin = make_user(db, "admin@example.com", name="Admin", role="admin")
    member = make_user(db, "member@example.com", name="Member")
    return admin, member


def reg(client: TestClient, headers: dict, **body):
    payload = {"email": "new@example.com", "name": "New", "password": strong(), **body}
    return client.post("/api/v1/auth/register-user", json=payload, headers=headers)


def test_admin_can_create_members_and_admins_and_audit_it(client: TestClient, db_session: Session) -> None:
    admin, _ = _admin_and_member(db_session)
    default = reg(client, bearer(admin))
    assert default.status_code == 201 and default.json()["role"] == "member" and default.json()["email_verified"]
    as_admin = reg(client, bearer(admin), email="second.admin@example.com", role="admin")
    assert as_admin.status_code == 201 and as_admin.json()["role"] == "admin"
    events = db_session.execute(select(AuditLog).where(AuditLog.diff["event"].astext == "user_registered_by_admin"))
    assert len(events.scalars().all()) == 2


def test_member_and_inactive_admin_and_anonymous_cannot_register_users(client: TestClient, db_session: Session) -> None:
    admin, member = _admin_and_member(db_session)
    assert reg(client, bearer(member)).status_code == 403
    assert reg(client, bearer(member), role="admin").status_code == 403  # no self-service escalation either
    admin.is_active = False
    db_session.commit()
    assert reg(client, bearer(admin)).status_code == 401
    assert reg(client, ANON).status_code == 404  # no BOOTSTRAP_TOKEN configured and users exist: closed
    assert db_session.execute(select(User).where(User.email == "new@example.com")).first() is None


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"role": "superuser"}, 422),
        ({"role": ""}, 422),
        ({"role": "ADMIN"}, 422),
        ({"is_active": False}, 422),
        ({"email": "bad-email"}, 422),
        ({"name": ""}, 422),
        ({"password": "short"}, 422),
        ({"email": "member@example.com"}, 409),
        ({"email": "MEMBER@example.com"}, 409),
    ],
)
def test_register_user_validation_matrix(client: TestClient, db_session: Session, extra: dict, code: int) -> None:
    admin, _ = _admin_and_member(db_session)
    before = len(db_session.execute(select(User)).scalars().all())
    assert reg(client, bearer(admin), **extra).status_code == code
    db_session.expire_all()
    assert len(db_session.execute(select(User)).scalars().all()) == before


def test_admin_accounts_need_the_stronger_password_policy_members_do_not(
    client: TestClient, db_session: Session
) -> None:
    admin, _ = _admin_and_member(db_session)
    medium = "abcdefghijkl1"  # 13 chars, two classes: fine for a member, refused for an admin
    assert reg(client, bearer(admin), email="m1@example.com", password=medium).status_code == 201
    assert reg(client, bearer(admin), email="a1@example.com", password=medium, role="admin").status_code == 422


def test_admin_cookie_session_needs_csrf_to_register_users(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "admin@example.com", role="admin")
    login(client, email="admin@example.com")
    assert reg(client, ANON).status_code == 403
    assert reg(client, csrf_headers(client)).status_code == 201


def test_register_user_from_a_foreign_origin_is_blocked(client: TestClient, db_session: Session) -> None:
    admin, _ = _admin_and_member(db_session)
    assert reg(client, {**bearer(admin), "Origin": "https://evil.example"}).status_code == 403


def test_a_stale_cookie_does_not_block_first_run_but_a_stale_one_is_401_afterwards(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from pydantic import SecretStr

    token = secrets.token_urlsafe(40)
    monkeypatch.setattr(get_settings(), "bootstrap_token", SecretStr(token))
    now = int(time.time())
    expired = jwt.encode(
        {"sub": str(uuid.uuid4()), "iat": now - 900, "exp": now - 5, "jti": "j", "typ": "access"},
        get_settings().jwt_secret,
        algorithm="HS256",
    )
    headers = {**ANON, "Authorization": f"Bearer {expired}", "X-Bootstrap-Token": token}
    first = reg(client, headers, email="root@example.com")
    assert first.status_code == 201 and first.json()["role"] == "admin"
    # Users exist now: an expired credential is reported as such (401), not as a closed endpoint (404).
    again = reg(
        client, {**ANON, "Authorization": f"Bearer {expired}", "X-Bootstrap-Token": token}, email="x@example.com"
    )
    assert again.status_code == 401


def test_bootstrap_token_cannot_be_used_by_a_signed_in_member_to_gain_admin(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from pydantic import SecretStr

    token = secrets.token_urlsafe(40)
    monkeypatch.setattr(get_settings(), "bootstrap_token", SecretStr(token))
    _, member = _admin_and_member(db_session)
    r = reg(client, {**bearer(member), "X-Bootstrap-Token": token}, role="admin")
    assert r.status_code == 403
    assert client.get("/api/v1/auth/config", headers=ANON).json()["setup_required"] is False


def test_auth_config_reports_setup_only_while_empty_and_configured(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from pydantic import SecretStr

    assert client.get("/api/v1/auth/config", headers=ANON).json() == {
        "signup_enabled": True,
        "setup_required": False,
        "username_login": False,  # no ADMIN_EMAIL configured in tests
    }
    monkeypatch.setattr(get_settings(), "bootstrap_token", SecretStr(secrets.token_urlsafe(30)))
    assert client.get("/api/v1/auth/config", headers=ANON).json()["setup_required"] is True
    make_user(db_session)
    assert client.get("/api/v1/auth/config", headers=ANON).json()["setup_required"] is False
