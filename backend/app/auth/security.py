"""Low-level auth primitives: Argon2id hashing, access-JWT encode/decode, opaque tokens, cookies, CSRF.

Nothing here touches the database; `app/auth/service.py` composes these with the tables.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, Response, status

from app.config import get_settings

ACCESS_COOKIE = "qs_access"
REFRESH_COOKIE = "qs_refresh"
CSRF_COOKIE = "qs_csrf"
CSRF_HEADER = "X-CSRF-Token"
# Path "/" (not just /api/v1/auth) so the Next.js middleware, which sees page navigations, can renew the session.
REFRESH_COOKIE_PATH = "/"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Argon2id with OWASP's "second" recommended profile (Password Storage Cheat Sheet): 19 MiB, 2 iterations,
# 1 lane. The hash string embeds the parameters, so they can be raised later and old hashes are upgraded on login.
_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)
# Verified against when the e-mail is unknown, so "no such user" costs the same as "wrong password".
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time-login")


def utcnow() -> datetime:
    return datetime.now(UTC)


# --- passwords ----------------------------------------------------------------------------------------

_COMMON = frozenset(
    "password passw0rd 123456789012 qwertyuiop12 letmein12345 administrator iloveyou1234 welcome12345 "
    "changeme1234 password1234 password12345 123456789abc qwerty123456 1q2w3e4r5t6y".split()
)


def password_problem(password: str, email: str | None = None) -> str | None:
    """A human-readable reason the password is unacceptable, or None. Length over composition (OWASP)."""
    s = get_settings()
    if len(password) < s.password_min_length:
        return f"Use at least {s.password_min_length} characters."
    if len(password) > s.password_max_length:
        return f"Use at most {s.password_max_length} characters."
    low = password.lower()
    if low in _COMMON or len(set(low)) < 5:
        return "That password is too easy to guess."
    if email and (low == email.lower() or low == email.split("@")[0].lower()):
        return "The password must not be your email address."
    return None


def admin_password_problem(password: str, email: str | None = None) -> str | None:
    """Stricter policy for admin accounts: the normal rules plus a longer minimum and at least 3 of 4 character
    classes (lower / upper / digit / symbol) or a long passphrase (20+ characters)."""
    problem = password_problem(password, email)
    if problem:
        return problem
    minimum = get_settings().admin_password_min_length
    if len(password) < minimum:
        return f"Admin passwords need at least {minimum} characters."
    classes = sum(
        bool(re.search(pattern, password)) for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 3 and len(password) < 20:
        return "Use a mix of upper/lower case, digits and symbols, or a passphrase of 20+ characters."
    return None


def hash_password_sync(password: str) -> str:
    """For CLIs / scripts (no event loop)."""
    return _hasher.hash(password)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time: an unknown/passwordless account still does one Argon2 verification."""
    target = password_hash or _DUMMY_HASH

    def _check() -> bool:
        try:
            return _hasher.verify(target, password) and password_hash is not None
        except (VerificationError, InvalidHashError):
            return False

    return await asyncio.to_thread(_check)


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def looks_like_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 320


# --- tokens -------------------------------------------------------------------------------------------


def new_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_access_token(user_id: uuid.UUID) -> tuple[str, int]:
    s = get_settings()
    now = utcnow()
    expires = now + timedelta(minutes=s.access_token_minutes)
    claims = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "iatm": int(now.timestamp() * 1000),  # ms precision: compared with sessions_valid_after
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": "access",
    }
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm), s.access_token_minutes * 60


def decode_access_token(token: str) -> dict:
    s = get_settings()
    try:
        claims = jwt.decode(
            token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["sub", "exp", "iat", "jti"]}
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    if claims.get("typ") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.")
    return claims


def extract_access_token(request: Request) -> tuple[str | None, bool]:
    """(token, came_from_cookie). An Authorization: Bearer header wins over the cookie."""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header.split(" ", 1)[1].strip() or None, False
    cookie = request.cookies.get(ACCESS_COOKIE)
    return (cookie or None), True


def peek_user_id(request: Request) -> uuid.UUID | None:
    """The user id in a valid access token (Bearer header or cookie) without hitting the DB; used to key
    per-user rate limits. Returns None when there is no usable token."""
    token, _via_cookie = extract_access_token(request)
    if not token:
        return None
    try:
        return uuid.UUID(decode_access_token(token)["sub"])
    except (HTTPException, ValueError, KeyError):
        return None


# --- cookies + CSRF -----------------------------------------------------------------------------------


def _cookie_kwargs() -> dict:
    s = get_settings()
    kw: dict = {"secure": s.cookies_secure, "samesite": s.cookie_samesite.lower()}
    if s.cookie_domain:
        kw["domain"] = s.cookie_domain
    return kw


def set_access_cookie(response: Response, access: str, max_age: int) -> None:
    response.set_cookie(ACCESS_COOKIE, access, max_age=max_age, httponly=True, path="/", **_cookie_kwargs())


def set_csrf_cookie(response: Response, csrf: str, max_age: int | None) -> None:
    # Readable by the page's JS on purpose (double-submit): the header must equal this cookie.
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, path="/", **_cookie_kwargs())


def set_refresh_cookie(response: Response, refresh: str, remember: bool) -> None:
    s = get_settings()
    max_age = s.refresh_days_remember * 86400 if remember else None  # None = session cookie
    response.set_cookie(
        REFRESH_COOKIE, refresh, max_age=max_age, httponly=True, path=REFRESH_COOKIE_PATH, **_cookie_kwargs()
    )


def clear_auth_cookies(response: Response) -> None:
    kw = _cookie_kwargs()
    response.delete_cookie(ACCESS_COOKIE, path="/", **kw)
    response.delete_cookie(CSRF_COOKIE, path="/", **kw)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH, **kw)


def allowed_origins() -> set[str]:
    s = get_settings()
    origins = {o.rstrip("/") for o in s.cors_origin_list}
    origins.add(s.frontend_url.rstrip("/"))
    return origins


def check_origin(request: Request) -> None:
    """Reject a browser request whose Origin (or, failing that, Referer) is not one of ours."""
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if not referer:
            return  # not a browser request (or a same-origin one that omits both): the CSRF token still applies
        m = re.match(r"^(https?://[^/]+)", referer)
        origin = m.group(1) if m else referer
    if origin.rstrip("/") not in allowed_origins():
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Cross-site request blocked.")


def verify_csrf(request: Request) -> None:
    """Double-submit check for cookie-authenticated unsafe requests: the X-CSRF-Token header must equal the
    qs_csrf cookie, and the Origin must be ours."""
    check_origin(request)
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Missing or invalid CSRF token.")


def client_ip(request: Request) -> str | None:
    """The caller's IP. Behind a trusted proxy/ALB, uvicorn's `--proxy-headers` (+ FORWARDED_ALLOW_IPS) already
    rewrites `request.client`; the raw X-Forwarded-For header is never trusted here."""
    return request.client.host if request.client else None
