# ruff: noqa: F811  (pytest fixtures imported from sibling test modules are re-declared as test arguments)
"""Races on the auth endpoints, driven by real threads against real Postgres: one winner, no duplicates, counters
that do not lose updates."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import security as sec
from app.config import get_settings
from app.models.auth import PasswordResetToken, RefreshToken
from app.models.user import User
from tests.test_auth import ANON, PASSWORD, csrf_headers, login, mails, make_user, token_from  # noqa: F401


def race(client: TestClient, n: int, job: Callable[[TestClient, int], object]) -> list:
    """Runs `job(own_client, i)` on n threads released together; returns the results in start order."""
    results: list = [None] * n
    barrier = threading.Barrier(n)

    def run(i: int) -> None:
        c = type(client)(client.app)
        barrier.wait()
        try:
            results[i] = job(c, i)
        except Exception as exc:  # noqa: BLE001
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    return results


def codes(results: list) -> list[int]:
    for r in results:
        assert not isinstance(r, Exception), repr(r)
    return sorted(r.status_code for r in results)


def test_simultaneous_signups_for_one_address_create_exactly_one_account(
    client: TestClient,
    db_session: Session,
    mails,
    monkeypatch,  # noqa: F811
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_signup", "1000/minute")
    results = race(
        client,
        6,
        lambda c, i: c.post(
            "/api/v1/auth/signup",
            json={"email": "twin@example.com", "name": f"T{i}", "password": PASSWORD},
            headers=ANON,
        ),
    )
    assert codes(results) == [201, 409, 409, 409, 409, 409]
    assert db_session.execute(select(func.count()).select_from(User)).scalar_one() == 1


def test_a_reset_token_can_be_spent_by_only_one_of_many_simultaneous_requests(
    client: TestClient,
    db_session: Session,
    mails,
    monkeypatch,  # noqa: F811
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_reset", "1000/hour")
    make_user(db_session)
    client.post("/api/v1/auth/forgot-password", json={"email": "alice@example.com"}, headers=ANON)
    token = token_from(mails, "Reset")
    passwords = [f"racing-passphrase-{i}-{secrets.token_hex(3)}" for i in range(6)]
    results = race(
        client,
        6,
        lambda c, i: c.post(
            "/api/v1/auth/reset-password", json={"token": token, "password": passwords[i]}, headers=ANON
        ),
    )
    assert codes(results) == [200, 400, 400, 400, 400, 400]
    winner = passwords[[r.status_code for r in results].index(200)]
    assert login(client, password=winner).status_code == 200  # exactly the winner's password is now valid
    client.cookies.clear()
    for p in passwords:
        if p != winner:
            assert login(client, password=p).status_code == 401


def test_a_verification_token_verifies_once_under_a_race(
    client: TestClient,
    db_session: Session,
    mails,
    monkeypatch,  # noqa: F811
) -> None:
    client.post("/api/v1/auth/signup", json={"email": "v@example.com", "name": "V", "password": PASSWORD}, headers=ANON)
    token = token_from(mails, "Verify")
    results = race(client, 5, lambda c, i: c.post("/api/v1/auth/verify-email", json={"token": token}, headers=ANON))
    assert codes(results) == [200, 400, 400, 400, 400]
    used = db_session.execute(select(PasswordResetToken.used_at)).scalars().all()
    assert len(used) == 1 and used[0] is not None


def test_parallel_wrong_passwords_never_lose_a_failure_count(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_login", "1000/minute")
    monkeypatch.setattr(s, "lockout_threshold", 1000)  # observe the raw counter, not the lock
    user = make_user(db_session)
    results = race(client, 8, lambda c, i: login(c, password=f"wrong-password-{i:02d}-x"))
    assert codes(results) == [401] * 8
    db_session.refresh(user)
    assert user.failed_logins == 8  # the increment is atomic in SQL: no lost updates


def test_lockout_engages_even_when_the_failures_arrive_together(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "rate_limit_login", "1000/minute")
    user = make_user(db_session)
    race(client, s.lockout_threshold + 3, lambda c, i: login(c, password=f"wrong-password-{i:02d}-x"))
    db_session.refresh(user)
    assert user.locked_until is not None
    assert login(client).status_code == 429  # even the right password is refused while locked


def test_parallel_refreshes_of_one_token_rotate_once_and_all_succeed(client: TestClient, db_session: Session) -> None:
    make_user(db_session)
    login(client)
    cookies = dict(client.cookies)
    csrf = client.cookies.get("qs_csrf")

    def job(c: TestClient, i: int):
        for name, value in cookies.items():
            c.cookies.set(name, value)
        return c.post("/api/v1/auth/refresh", headers={**ANON, "X-CSRF-Token": csrf})

    results = race(client, 5, job)
    assert codes(results) == [200] * 5  # inside the grace window nobody is treated as a thief
    db_session.expire_all()
    rows = db_session.execute(select(RefreshToken)).scalars().all()
    assert len(rows) == 2  # the original (used) + exactly ONE successor
    assert sum(1 for r in rows if r.used_at is None) == 1 and all(r.revoked_at is None for r in rows)


def test_admin_creating_the_same_user_in_parallel_yields_one_account(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_register_user", "1000/minute")
    admin = make_user(db_session, "admin@example.com", role="admin")
    headers = {**ANON, "Authorization": f"Bearer {sec.create_access_token(admin.id)[0]}"}
    results = race(
        client,
        5,
        lambda c, i: c.post(
            "/api/v1/auth/register-user",
            json={"email": "dup@example.com", "name": "D", "password": f"Aa1!{secrets.token_urlsafe(16)}"},
            headers=headers,
        ),
    )
    assert codes(results) == [201, 409, 409, 409, 409]
    assert (
        db_session.execute(select(func.count()).select_from(User).where(User.email == "dup@example.com")).scalar_one()
        == 1
    )


def test_logout_all_during_parallel_requests_leaves_no_valid_token_behind(
    client: TestClient, db_session: Session
) -> None:
    make_user(db_session)
    token = login(client).json()["access_token"]
    assert client.post("/api/v1/auth/logout-all", headers=csrf_headers(client)).status_code == 200
    results = race(
        client,
        6,
        lambda c, i: c.get("/api/v1/me", headers={**ANON, "Authorization": f"Bearer {token}"}),
    )
    assert codes(results) == [401] * 6
