"""Operator CLI: give an account a password (and mark its e-mail verified), optionally creating it or changing its role.

For operators with shell access to the deployment - the way to recover an admin, claim an account that was created
without a password (rows made by the seed script or older sign-ups), or add an admin out of band. In normal use:

* local dev: set ADMIN_EMAIL / ADMIN_PASSWORD in infra/.env and the first admin is created on startup;
* production: use the one-time BOOTSTRAP_TOKEN flow (POST /api/v1/auth/register-user), then admins create users.

    python -m app.scripts.set_password someone@example.com                      # prompts (hidden) twice
    python -m app.scripts.set_password admin@example.com --create --role admin --name "Admin"
    python -m app.scripts.set_password a@b.com --password-stdin < pw.txt        # non-interactive

The password never appears on the command line and is never printed or logged. Existing sessions of the account
are revoked. Admin passwords must satisfy the stricter admin policy.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime

from sqlalchemy import select, update

from app.auth.security import admin_password_problem, hash_password_sync, normalize_email, password_problem
from app.config import get_settings
from app.db import SyncSessionLocal
from app.models.auth import RefreshToken
from app.models.user import ASSIGNABLE_ROLES, ROLE_ADMIN, ROLE_MEMBER, User


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set (or create) an account password.")
    parser.add_argument("email")
    parser.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    parser.add_argument("--create", action="store_true", help="create the account when it does not exist")
    parser.add_argument("--name", default=None, help="display name for --create")
    parser.add_argument(
        "--role", choices=ASSIGNABLE_ROLES, default=None, help="set the role (default: keep; member on --create)"
    )
    parser.add_argument(
        "--allow-weak",
        action="store_true",
        help="skip the password-strength policy (refused when ENV=production; for local dev logins only)",
    )
    args = parser.parse_args(argv)
    if args.allow_weak and get_settings().is_production:
        print("--allow-weak is refused when ENV=production.", file=sys.stderr)
        return 1

    email = normalize_email(args.email)
    if args.password_stdin:
        password = sys.stdin.read().rstrip("\r\n")
    else:
        password = getpass.getpass("New password: ")
        if getpass.getpass("Repeat password: ") != password:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    with SyncSessionLocal() as db:
        current = db.execute(select(User.role).where(User.email == email)).scalar_one_or_none()
    role = args.role or current or ROLE_MEMBER
    if args.allow_weak:
        problem = None if password else "empty password"
    else:
        problem = admin_password_problem(password, email) if role == ROLE_ADMIN else password_problem(password, email)
    if problem:
        print(f"Password rejected: {problem}", file=sys.stderr)
        return 1

    with SyncSessionLocal() as db:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            if not args.create:
                print(f"No account with email {email}. Pass --create to create it.", file=sys.stderr)
                return 1
            user = User(email=email, name=args.name or email.split("@")[0], role=role)
            db.add(user)
            db.flush()
        user.password_hash = hash_password_sync(password)
        user.role = role
        user.email_verified_at = user.email_verified_at or datetime.now(UTC)
        user.failed_logins = 0
        user.locked_until = None
        user.sessions_valid_after = datetime.now(UTC)
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        db.commit()
        print(f"Password set for {email}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
