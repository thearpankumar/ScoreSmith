"""Route inventory: every endpoint under /api/v1 is either public on purpose or demands a signed-in user, and every
id-taking endpoint is covered by the cross-user matrix (tests/test_authz_isolation.py). A NEW route fails here until
somebody decides which of the two it is, so an unprotected endpoint cannot slip in unnoticed."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from tests.test_authz_isolation import World, _cases, _h

H_ANON = {"X-Test-Anonymous": "1"}

# Public by design (everything else must answer 401 without credentials).
PUBLIC = {
    ("POST", "/api/v1/auth/signup"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),  # authenticated by the refresh cookie + CSRF
    ("POST", "/api/v1/auth/logout"),
    ("GET", "/api/v1/auth/config"),
    ("POST", "/api/v1/auth/register-user"),  # bootstrap token / admin: answers 404 when closed
    ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"),
    ("POST", "/api/v1/auth/verify-email"),
    ("GET", "/api/v1/auth/providers"),
    ("GET", "/api/v1/auth/oauth/{provider_id}/start"),
    ("GET", "/api/v1/auth/oauth/{provider_id}/callback"),
}

# Authenticated but with no resource id to hijack, or covered by a dedicated isolation test.
NO_FOREIGN_ID = {
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/me"),
    ("PATCH", "/api/v1/me"),
    ("POST", "/api/v1/auth/logout-all"),
    ("POST", "/api/v1/auth/resend-verification"),
    ("GET", "/api/v1/scorecards"),  # list scoping: test_lists_only_contain_the_callers_own_rows
    ("POST", "/api/v1/scorecards"),  # ownership from token: test_ownership_comes_from_the_token...
    ("GET", "/api/v1/evaluations"),
    ("GET", "/api/v1/chat/sessions"),
    ("POST", "/api/v1/scorecards/suggest-similar"),  # test_suggest_similar_only_suggests_the_callers_own_scorecards
    ("POST", "/api/v1/scorecards/validate-formula"),  # pure computation, no stored data
    ("POST", "/api/v1/evaluations/export"),  # test_export_never_includes_another_users_evaluations
    ("POST", "/api/v1/evaluations/ai/uploads"),  # test_upload_keys_are_bound_to_the_user_who_got_them
    ("POST", "/api/v1/evaluations/ai/uploads/complete"),
    ("POST", "/api/v1/evaluations/ai/uploads/abort"),
    ("POST", "/api/v1/evaluations/ai/batches/parse"),
    ("POST", "/api/v1/chat/sessions"),  # creates a session for the caller (+ foreign target covered in the matrix)
    # --- sharing / notifications / RBAC (tests/test_sharing.py, test_notifications.py, test_eval_page.py,
    # test_admin_users.py, test_user_slots.py): scoped to the caller, or admin-only (403) ---
    ("GET", "/api/v1/invitations"),  # the caller's own pending invitations
    ("GET", "/api/v1/notifications"),  # test_inbox_is_scoped_to_its_owner
    ("GET", "/api/v1/notifications/unread-count"),
    ("POST", "/api/v1/notifications/read-all"),
    ("GET", "/api/v1/me/slots"),
    ("POST", "/api/v1/me/password"),  # own password only (test_users_change_their_own_password)
    ("GET", "/api/v1/chat/shared"),  # the caller's own shared-with-me list (test_chat_shares.py)
    ("GET", "/api/v1/evaluations/page"),  # test_access_follows_the_chart
    ("POST", "/api/v1/evaluations/refresh"),  # test_refresh_returns_current_state_of_visible_rows_only
    ("POST", "/api/v1/evaluations/bulk-delete"),  # test_bulk_delete_skips_running_and_foreign_rows
    # --- chart trash (tests/test_chart_trash.py): only ever the caller's OWN trashed charts; foreign ids -> 404 ---
    ("GET", "/api/v1/scorecards/trash"),
    ("POST", "/api/v1/scorecards/trash/restore"),
    ("POST", "/api/v1/scorecards/trash/purge"),
    ("POST", "/api/v1/scorecards/trash/empty"),
    ("GET", "/api/v1/admin/users"),  # admin-only: test_every_admin_endpoint_is_403_for_a_normal_user
    ("POST", "/api/v1/admin/users"),
    ("PATCH", "/api/v1/admin/users/{user_id}"),
    ("POST", "/api/v1/admin/users/{user_id}/password"),
    ("POST", "/api/v1/admin/users/{user_id}/deactivate"),
    ("POST", "/api/v1/admin/users/{user_id}/reactivate"),
    ("DELETE", "/api/v1/admin/users/{user_id}"),
}


def _template(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def _routes() -> list[tuple[str, str]]:
    found = []
    for r in app.routes:
        if isinstance(r, APIRoute) and r.path.startswith("/api/v1"):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                found.append((m, r.path))
    return found


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _m: str(uuid.uuid4()), path)


def test_every_non_public_route_rejects_anonymous_callers(client: TestClient) -> None:
    unprotected = []
    for method, path in _routes():
        if (method, path) in PUBLIC:
            continue
        r = client.request(method, _concrete(path), json={}, headers=H_ANON)
        if r.status_code != 401:
            unprotected.append(f"{method} {path} -> {r.status_code}")
    assert not unprotected, "\n".join(unprotected)


def test_public_routes_list_is_current_and_does_not_hide_a_protected_route() -> None:
    all_routes = set(_routes())
    stale = PUBLIC - all_routes
    assert not stale, f"PUBLIC lists routes that no longer exist: {sorted(stale)}"
    stale = NO_FOREIGN_ID - all_routes
    assert not stale, f"NO_FOREIGN_ID lists routes that no longer exist: {sorted(stale)}"


def test_every_id_taking_route_is_in_the_cross_user_matrix(db_session: Session) -> None:
    world = World(db_session)
    covered = {
        (m, _template(re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "{}", p)))
        for m, p, _b in _cases(world)
    }
    missing = []
    for method, path in _routes():
        if (method, path) in PUBLIC or (method, path) in NO_FOREIGN_ID:
            continue
        if (method, _template(path)) not in covered:
            missing.append(f"{method} {path}")
    assert not missing, (
        "Add these to _cases() in test_authz_isolation.py (or to NO_FOREIGN_ID with a reason):\n" + "\n".join(missing)
    )


@pytest.mark.parametrize("path", ["/api/v1/scorecards", "/api/v1/evaluations", "/api/v1/chat/sessions"])
def test_pagination_boundaries(client: TestClient, db_session: Session, path: str) -> None:
    world = World(db_session)
    h = _h(world.b.id)
    assert client.get(f"{path}?limit=1", headers=h).status_code == 200
    assert client.get(f"{path}?limit=200", headers=h).status_code == 200  # the documented maximum
    assert client.get(f"{path}?limit=201", headers=h).status_code == 422
    assert client.get(f"{path}?limit=abc", headers=h).status_code == 422
    assert client.get(f"{path}?skip=0&limit=1", headers=h).json() != []
    assert client.get(f"{path}?skip=1&limit=100", headers=h).json() == []  # B owns exactly one of each
    assert client.get(f"{path}?skip=100000", headers=h).json() == []


def test_unknown_route_and_method_are_404_405_not_500(client: TestClient, db_session: Session) -> None:
    world = World(db_session)
    h = _h(world.b.id)
    assert client.get("/api/v1/nope", headers=h).status_code == 404
    assert client.put(f"/api/v1/scorecards/{world.sc.id}", json={}, headers=h).status_code == 405
    assert client.get("/api/v1/scorecards/not-a-uuid", headers=h).status_code == 422
