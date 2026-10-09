"""OAuth sign-in (Google, Microsoft, GitHub): authorization-code flow with `state` and PKCE.

A provider is enabled only when its client id AND secret are configured (see config.py); until then
`GET /auth/providers` reports it disabled (the login page shows the button disabled) and the start endpoint
answers 501. Accounts are linked by *verified* e-mail only: Google and GitHub assert e-mail ownership; Microsoft's
`email` claim is not a proof of ownership (the "nOAuth" class of account-takeover bugs), so a Microsoft login
never attaches itself to an existing account by e-mail - it can only sign in an already-linked identity or create
a brand-new account.

This module is exercised in tests for the not-configured and invalid-state paths; the provider round-trips need
real client credentials and have not been run against live providers.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.auth import security as sec
from app.auth import service
from app.config import Settings, get_settings
from app.db import get_db
from app.models.auth import OAuthIdentity
from app.models.enums import AuditAction
from app.models.user import User
from app.ratelimit import rate_limit
from app.schemas.user import ProviderInfo, ProvidersResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

STATE_COOKIE = "qs_oauth"
STATE_TTL_SECONDS = 600


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    authorize_url: str
    token_url: str
    scope: str
    trust_email_for_linking: bool


PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        "google",
        "Google",
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "openid email profile",
        True,
    ),
    "github": Provider(
        "github",
        "GitHub",
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        "read:user user:email",
        True,
    ),
    "microsoft": Provider(
        "microsoft",
        "Microsoft",
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "openid email profile",
        False,
    ),
}


def _credentials(s: Settings, provider_id: str) -> tuple[str, str]:
    return getattr(s, f"oauth_{provider_id}_client_id"), getattr(s, f"oauth_{provider_id}_client_secret")


def provider_enabled(s: Settings, provider_id: str) -> bool:
    client_id, secret = _credentials(s, provider_id)
    return bool(client_id and secret)


def _redirect_uri(provider_id: str) -> str:
    return f"{get_settings().oauth_redirect_base.rstrip('/')}/api/v1/auth/oauth/{provider_id}/callback"


def _login_redirect(error: str | None = None) -> RedirectResponse:
    base = get_settings().frontend_url.rstrip("/")
    return RedirectResponse(
        f"{base}/login?error={error}" if error else f"{base}/", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    s = get_settings()
    return ProvidersResponse(
        providers=[ProviderInfo(id=p.id, name=p.name, enabled=provider_enabled(s, p.id)) for p in PROVIDERS.values()]
    )


def _get_provider(provider_id: str) -> Provider:
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown sign-in provider.")
    if not provider_enabled(get_settings(), provider_id):
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"Sign in with {provider.name} is not configured on this server yet.",
        )
    return provider


@router.get(
    "/oauth/{provider_id}/start",
    dependencies=[Depends(rate_limit("oauth", lambda s: s.rate_limit_login))],
)
async def oauth_start(provider_id: str, remember: bool = False) -> RedirectResponse:
    provider = _get_provider(provider_id)
    s = get_settings()
    client_id, _ = _credentials(s, provider_id)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(provider_id),
        "response_type": "code",
        "scope": provider.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(f"{provider.authorize_url}?{urlencode(params)}", status_code=status.HTTP_302_FOUND)
    # The state + PKCE verifier live in a short-lived signed httpOnly cookie, bound to this browser.
    packed = jwt.encode(
        {
            "p": provider_id,
            "s": state,
            "v": verifier,
            "r": remember,
            "exp": int(sec.utcnow().timestamp()) + STATE_TTL_SECONDS,
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    response.set_cookie(
        STATE_COOKIE,
        packed,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        path="/api/v1/auth/oauth",
        secure=s.cookies_secure,
        samesite="lax",
    )
    return response


async def _fetch_profile(provider: Provider, client_id: str, secret: str, code: str, verifier: str) -> dict:
    """Exchanges the code and returns {subject, email, email_verified, name}."""
    async with httpx.AsyncClient(timeout=15.0) as http:
        token_resp = await http.post(
            provider.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(provider.id),
                "client_id": client_id,
                "client_secret": secret,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
        token_resp.raise_for_status()
        access = token_resp.json().get("access_token")
        if not access:
            raise ValueError("no access token")
        auth = {"Authorization": f"Bearer {access}", "Accept": "application/json"}
        if provider.id == "github":
            user = (await http.get("https://api.github.com/user", headers=auth)).json()
            emails = (await http.get("https://api.github.com/user/emails", headers=auth)).json()
            primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
            return {
                "subject": str(user["id"]),
                "email": (primary or {}).get("email"),
                "email_verified": primary is not None,
                "name": user.get("name") or user.get("login") or "",
            }
        url = (
            "https://openidconnect.googleapis.com/v1/userinfo"
            if provider.id == "google"
            else "https://graph.microsoft.com/oidc/userinfo"
        )
        info = (await http.get(url, headers=auth)).json()
        return {
            "subject": str(info["sub"]),
            "email": info.get("email"),
            "email_verified": bool(info.get("email_verified")) if provider.id == "google" else False,
            "name": info.get("name") or "",
        }


@router.get("/oauth/{provider_id}/callback")
async def oauth_callback(
    provider_id: str,
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    provider = _get_provider(provider_id)
    s = get_settings()
    response = _login_redirect()
    response.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oauth")
    if error or not code or not state:
        return _fail(response, "oauth_denied")
    try:
        packed = jwt.decode(request.cookies.get(STATE_COOKIE, ""), s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.PyJWTError:
        return _fail(response, "oauth_state")
    if packed.get("p") != provider_id or not secrets.compare_digest(str(packed.get("s")), state):
        return _fail(response, "oauth_state")
    client_id, secret = _credentials(s, provider_id)
    try:
        profile = await _fetch_profile(provider, client_id, secret, code, packed["v"])
    except Exception:  # noqa: BLE001
        logger.warning("OAuth exchange with %s failed.", provider_id, exc_info=True)
        return _fail(response, "oauth_failed")

    identity = (
        await db.execute(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == provider_id, OAuthIdentity.subject == profile["subject"]
            )
        )
    ).scalar_one_or_none()
    user: User | None = await db.get(User, identity.user_id) if identity else None
    event = "oauth_login"
    if user is None:
        email = sec.normalize_email(profile["email"] or "")
        if not email or not sec.looks_like_email(email):
            return _fail(response, "oauth_no_email")
        existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is not None:
            if not (provider.trust_email_for_linking and profile["email_verified"]):
                return _fail(response, "oauth_account_exists")
            user = existing
            event = "oauth_linked"
            if existing.email_verified_at is None:
                # Pre-hijack guard: the local account was created by SIGNUP, which does not prove ownership of
                # the address. Whoever registered it may be an attacker who chose the password, so linking must
                # not hand the real owner an account that someone else can still sign in to: drop the password
                # (the owner sets one through "Forgot password") and end every session issued so far.
                existing.password_hash = None
                await service.revoke_all_sessions(db, existing)
                existing.sessions_valid_after = sec.utcnow() - timedelta(milliseconds=50)
        else:
            if not get_settings().signup_allowed:
                return _fail(response, "oauth_signup_disabled")
            user = User(
                email=email,
                name=(profile["name"] or email.split("@")[0])[:200],
                role="member",
                email_verified_at=sec.utcnow() if profile["email_verified"] else None,
            )
            db.add(user)
            await db.flush()
            event = "oauth_signup"
        db.add(OAuthIdentity(user_id=user.id, provider=provider_id, subject=profile["subject"], email=email))
    if not user.is_active:
        return _fail(response, "oauth_disabled")
    if event == "oauth_linked" and user.email_verified_at is None:
        user.email_verified_at = sec.utcnow()
    try:
        await service.start_session(db, user, request, response, remember=bool(packed.get("r")))
        audit(
            db,
            request,
            actor_id=user.id,
            entity_type="auth",
            entity_id=user.id,
            action=AuditAction.CREATE,
            event=event,
            diff={"provider": provider_id},
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _fail(response, "oauth_failed")
    return response


def _fail(response: RedirectResponse, code: str) -> RedirectResponse:
    failed = _login_redirect(code)
    failed.delete_cookie(STATE_COOKIE, path="/api/v1/auth/oauth")
    return failed
