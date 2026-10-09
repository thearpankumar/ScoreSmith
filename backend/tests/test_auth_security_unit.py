"""Pure unit tests for app/auth/security.py and app/ratelimit.py primitives (no HTTP, no database writes)."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.auth import security as sec
from app.config import get_settings
from app.ratelimit import _MemoryWindow, rate_limit, reset_rate_limits, within_limit


def _request(headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None, method="POST") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        raw.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    return Request(
        {"type": "http", "method": method, "path": "/", "headers": raw, "query_string": b"", "client": ("9.9.9.9", 1)}
    )


def _forge(claims: dict, secret: str | None = None, alg: str = "HS256") -> str:
    return jwt.encode(claims, secret or get_settings().jwt_secret, algorithm=alg)


def _claims(**over) -> dict:
    now = int(time.time())
    base = {"sub": str(uuid.uuid4()), "iat": now, "exp": now + 60, "jti": "x", "typ": "access"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


# --- password policy: boundaries ----------------------------------------------------------------------


@pytest.mark.parametrize(("length", "ok"), [(11, False), (12, True), (13, True), (128, True), (129, False)])
def test_password_length_boundaries(length: int, ok: bool) -> None:
    password = ("aB3$xY7!qZ" * 20)[:length]
    assert (sec.password_problem(password) is None) is ok


def test_password_policy_rejects_common_low_entropy_and_email_like() -> None:
    assert sec.password_problem("Password1234") is not None  # case-insensitive common list
    assert sec.password_problem("abababababab") is not None  # fewer than five distinct characters
    assert sec.password_problem("alice@example.com", "Alice@Example.com") is not None
    assert sec.password_problem("alice-the-user", "alice-the-user@example.com") is not None  # local part
    assert sec.password_problem("a-fine-passphrase", "bob@example.com") is None


@pytest.mark.parametrize(
    ("password", "ok"),
    [
        ("Short1!aB", False),  # under 14
        ("Abcdefgh12345!", True),  # 14, three classes
        ("abcdefghijklmn1", False),  # 15 chars but only two classes and < 20
        ("abcdefghijklmnopqrstuvwx", True),  # 20+ passphrase needs no classes
        ("ABCDEFGHIJKLMN12", False),  # two classes
    ],
)
def test_admin_password_policy(password: str, ok: bool) -> None:
    assert (sec.admin_password_problem(password, "a@example.com") is None) is ok


def test_email_helpers() -> None:
    assert sec.normalize_email("  Foo@Bar.COM \n") == "foo@bar.com"
    for good in ("a@b.co", "first.last+tag@sub.example.org"):
        assert sec.looks_like_email(good)
    for bad in ("", "no-at-sign", "a@b", "a b@c.de", "@x.com", "a@" + "b" * 320 + ".com"):
        assert not sec.looks_like_email(bad)


# --- hashing ------------------------------------------------------------------------------------------


def test_argon2_hash_verifies_and_rejects() -> None:
    pw = secrets.token_urlsafe(16)
    h = sec.hash_password_sync(pw)
    assert h.startswith("$argon2id$") and pw not in h
    assert h != sec.hash_password_sync(pw)  # salted
    assert asyncio.run(sec.verify_password(pw, h)) is True
    assert asyncio.run(sec.verify_password(pw + "x", h)) is False
    assert asyncio.run(sec.verify_password("", h)) is False


def test_verify_password_is_false_for_passwordless_and_corrupt_hashes() -> None:
    assert asyncio.run(sec.verify_password("anything-at-all-1", None)) is False
    assert asyncio.run(sec.verify_password("anything-at-all-1", "not-a-hash")) is False


def test_needs_rehash_flags_weaker_parameters_and_garbage() -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("whatever-pw")
    assert sec.needs_rehash(weak) is True
    assert sec.needs_rehash("garbage") is True
    assert sec.needs_rehash(sec.hash_password_sync("whatever-pw")) is False


def test_opaque_tokens_are_unique_and_hashed_deterministically() -> None:
    tokens = {sec.new_opaque_token() for _ in range(50)}
    assert len(tokens) == 50 and all(len(t) >= 60 for t in tokens)
    t = next(iter(tokens))
    assert sec.hash_token(t) == sec.hash_token(t) and len(sec.hash_token(t)) == 64 and t not in sec.hash_token(t)


# --- access JWT ---------------------------------------------------------------------------------------


def test_access_token_roundtrip_has_required_claims() -> None:
    uid = uuid.uuid4()
    token, expires_in = sec.create_access_token(uid)
    claims = sec.decode_access_token(token)
    assert claims["sub"] == str(uid) and claims["typ"] == "access" and expires_in == 900
    assert claims["exp"] - claims["iat"] == 900 and claims["iatm"] >= claims["iat"] * 1000
    assert sec.create_access_token(uid)[0] != token  # unique jti each time


@pytest.mark.parametrize(
    "token_factory",
    [
        lambda: _forge(_claims(exp=int(time.time()) - 5)),  # expired
        lambda: _forge(_claims(), secret=secrets.token_urlsafe(40)),  # signed with another key
        lambda: _forge(_claims(typ="refresh")),  # wrong token type
        lambda: _forge(_claims(typ=None)),  # type missing
        lambda: _forge(_claims(sub=None)),  # required claim missing
        lambda: _forge(_claims(jti=None)),
        lambda: _forge(_claims(exp=None)),
        lambda: _forge(_claims(iat=None)),
        lambda: jwt.encode(_claims(), key="", algorithm="none"),  # alg=none downgrade
        lambda: _forge(_claims(), alg="HS384"),  # different HMAC algorithm than configured
        lambda: "",  # empty
        lambda: "not.a.jwt",
        lambda: sec.create_access_token(uuid.uuid4())[0][:-3] + "abc",  # tampered signature
    ],
)
def test_decode_rejects_every_malformed_forged_or_wrong_type_token(token_factory) -> None:
    with pytest.raises(HTTPException) as err:
        sec.decode_access_token(token_factory())
    assert err.value.status_code == 401


def test_tampered_payload_is_rejected() -> None:
    token, _ = sec.create_access_token(uuid.uuid4())
    head, body, sig = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims["sub"] = str(uuid.uuid4())
    forged_body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(HTTPException):
        sec.decode_access_token(f"{head}.{forged_body}.{sig}")


def test_extract_access_token_prefers_bearer_header_over_cookie() -> None:
    both = _request({"Authorization": "Bearer hdr-token"}, {sec.ACCESS_COOKIE: "cookie-token"})
    assert sec.extract_access_token(both) == ("hdr-token", False)
    assert sec.extract_access_token(_request({"Authorization": "bearer lower"})) == ("lower", False)
    assert sec.extract_access_token(_request(cookies={sec.ACCESS_COOKIE: "c"})) == ("c", True)
    assert sec.extract_access_token(_request()) == (None, True)
    # An empty bearer value yields no token (the header wins; no silent fallback to a cookie)
    assert sec.extract_access_token(_request({"Authorization": "Bearer "}, {sec.ACCESS_COOKIE: "c"}))[0] is None
    # Basic auth is ignored; the cookie is used
    assert sec.extract_access_token(_request({"Authorization": "Basic abc"}, {sec.ACCESS_COOKIE: "c"})) == ("c", True)


def test_peek_user_id_only_trusts_valid_tokens() -> None:
    uid = uuid.uuid4()
    good, _ = sec.create_access_token(uid)
    assert sec.peek_user_id(_request({"Authorization": f"Bearer {good}"})) == uid
    assert sec.peek_user_id(_request(cookies={sec.ACCESS_COOKIE: good})) == uid
    assert sec.peek_user_id(_request({"Authorization": "Bearer junk"})) is None
    assert sec.peek_user_id(_request()) is None
    assert sec.peek_user_id(_request({"Authorization": f"Bearer {_forge(_claims(sub='not-a-uuid'))}"})) is None


# --- CSRF + origin ------------------------------------------------------------------------------------


def test_check_origin_accepts_ours_and_rejects_foreign() -> None:
    ours = get_settings().frontend_url.rstrip("/")
    sec.check_origin(_request({"Origin": ours}))
    sec.check_origin(_request({"Origin": ours + "/"}))  # trailing slash tolerated
    sec.check_origin(_request())  # neither Origin nor Referer: not a browser request
    sec.check_origin(_request({"Referer": ours + "/some/page?x=1"}))
    for bad in ("https://evil.example", ours + ".evil.example", "null", "http://localhost:1"):
        with pytest.raises(HTTPException) as err:
            sec.check_origin(_request({"Origin": bad}))
        assert err.value.status_code == 403
    with pytest.raises(HTTPException):
        sec.check_origin(_request({"Referer": "https://evil.example/x"}))
    with pytest.raises(HTTPException):  # Origin wins over a friendly Referer
        sec.check_origin(_request({"Origin": "https://evil.example", "Referer": ours + "/"}))


def test_verify_csrf_requires_matching_cookie_and_header() -> None:
    ours = get_settings().frontend_url
    tok = secrets.token_urlsafe(24)

    def call(header: str | None, cookie: str | None, origin: str | None = ours) -> None:
        headers = {"Origin": origin} if origin else {}
        if header is not None:
            headers[sec.CSRF_HEADER] = header
        sec.verify_csrf(_request(headers, {sec.CSRF_COOKIE: cookie} if cookie is not None else None))

    call(tok, tok)
    for header, cookie in ((None, tok), (tok, None), ("", ""), (tok, tok + "x"), (tok.upper(), tok)):
        with pytest.raises(HTTPException) as err:
            call(header, cookie)
        assert err.value.status_code == 403
    with pytest.raises(HTTPException):  # matching token but hostile origin
        call(tok, tok, origin="https://evil.example")


def test_cookie_helpers_set_the_right_flags_and_clear_all_three() -> None:
    r = Response()
    sec.set_access_cookie(r, "A", 900)
    sec.set_refresh_cookie(r, "R", remember=True)
    sec.set_csrf_cookie(r, "C", 100)
    cookies = {h.split("=", 1)[0]: h for h in r.headers.getlist("set-cookie")}
    assert "httponly" in cookies[sec.ACCESS_COOKIE].lower() and "Max-Age=900" in cookies[sec.ACCESS_COOKIE]
    assert "httponly" in cookies[sec.REFRESH_COOKIE].lower()
    assert f"Max-Age={get_settings().refresh_days_remember * 86400}" in cookies[sec.REFRESH_COOKIE]
    assert "httponly" not in cookies[sec.CSRF_COOKIE].lower()
    session = Response()
    sec.set_refresh_cookie(session, "R", remember=False)
    assert "max-age" not in session.headers["set-cookie"].lower()  # a session cookie
    cleared = Response()
    sec.clear_auth_cookies(cleared)
    gone = cleared.headers.getlist("set-cookie")
    assert len(gone) == 3 and all("Max-Age=0" in g for g in gone)


def test_client_ip_ignores_forwarded_for_header() -> None:
    assert sec.client_ip(_request({"X-Forwarded-For": "1.2.3.4"})) == "9.9.9.9"


# --- in-process rate limit window ---------------------------------------------------------------------


def test_memory_window_allows_exactly_the_limit_then_reports_retry_after() -> None:
    from limits import parse

    win, item = _MemoryWindow(), parse("3/minute")
    assert [win.hit(item, "k")[0] for _ in range(3)] == [True, True, True]
    allowed, retry = win.hit(item, "k")
    assert allowed is False and 1 <= retry <= 61
    assert win.hit(item, "other")[0] is True  # buckets are independent
    win.reset()
    assert win.hit(item, "k")[0] is True


def test_memory_window_slides(monkeypatch) -> None:
    from limits import parse

    clock = [1000.0]
    monkeypatch.setattr("app.ratelimit.time.time", lambda: clock[0])
    win, item = _MemoryWindow(), parse("2/minute")
    assert win.hit(item, "k")[0] and win.hit(item, "k")[0] and not win.hit(item, "k")[0]
    clock[0] += 61
    assert win.hit(item, "k")[0]


def test_rate_limit_dependency_buckets_per_user_vs_ip_and_can_be_disabled(monkeypatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "redis_url", "")
    reset_rate_limits()
    dep_ip = rate_limit("t-ip", lambda _s: "2/minute")
    dep_user = rate_limit("t-user", lambda _s: "1/minute", per_user=True)
    u1, u2 = sec.create_access_token(uuid.uuid4())[0], sec.create_access_token(uuid.uuid4())[0]

    async def go():
        anon = _request()
        await dep_ip(anon)
        await dep_ip(anon)
        with pytest.raises(HTTPException) as err:
            await dep_ip(anon)
        assert err.value.status_code == 429 and int(err.value.headers["Retry-After"]) >= 1
        await dep_user(_request({"Authorization": f"Bearer {u1}"}))
        await dep_user(_request({"Authorization": f"Bearer {u2}"}))  # another user: own bucket
        with pytest.raises(HTTPException):
            await dep_user(_request({"Authorization": f"Bearer {u1}"}))
        # Without a token, per-user limits fall back to the client IP bucket.
        await dep_user(_request())
        with pytest.raises(HTTPException):
            await dep_user(_request())
        monkeypatch.setattr(s, "rate_limit_enabled", False)
        for _ in range(10):
            await dep_ip(anon)  # disabled: never raises
        assert await within_limit("t", "1/minute", "x") and await within_limit("t", "1/minute", "x")

    asyncio.run(go())
    reset_rate_limits()


def test_within_limit_counts_per_identifier(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "redis_url", "")
    reset_rate_limits()

    async def go():
        assert [await within_limit("w", "2/hour", "a") for _ in range(3)] == [True, True, False]
        assert await within_limit("w", "2/hour", "b") is True
        assert await within_limit("w2", "2/hour", "a") is True

    asyncio.run(go())
    reset_rate_limits()


def test_rate_limit_falls_back_to_memory_when_redis_errors(monkeypatch) -> None:
    """A dead Redis must never block requests: the in-process limiter takes over and Redis is skipped for a while."""
    import app.ratelimit as rl

    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(rl, "_redis_limiter", None)
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    reset_rate_limits()

    class Boom:
        async def hit(self, *_a, **_k):
            raise ConnectionError("redis is down")

    monkeypatch.setattr(rl, "_redis", lambda: Boom() if rl._redis_down_until == 0.0 else None)

    async def go():
        assert await within_limit("fo", "1/minute", "z") is True  # error -> memory limiter allowed it
        assert rl._redis_down_until > 0.0  # breaker armed
        assert await within_limit("fo", "1/minute", "z") is False  # memory limiter now enforces

    asyncio.run(go())
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    reset_rate_limits()


def test_utcnow_is_timezone_aware() -> None:
    now = sec.utcnow()
    assert now.tzinfo is UTC and abs(now - datetime.now(UTC)) < timedelta(seconds=2)


def test_rate_limit_survives_a_redis_storage_that_cannot_even_be_built(monkeypatch) -> None:
    """Regression: with REDIS_URL set but the storage driver unusable, every rate-limited route used to raise
    (HTTP 500 on login / signup). It must fall back to the in-process window instead."""
    import app.ratelimit as rl

    def broken(*_a, **_k):
        raise RuntimeError("driver prerequisite not available")

    monkeypatch.setattr(get_settings(), "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(rl, "_redis_limiter", None)
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    monkeypatch.setattr(rl, "RedisStorage", broken)
    reset_rate_limits()

    async def go():
        assert [await within_limit("nb", "2/minute", "q") for _ in range(3)] == [True, True, False]

    asyncio.run(go())
    monkeypatch.setattr(rl, "_redis_down_until", 0.0)
    reset_rate_limits()
