"""Guard: no secrets in the repository. Scans every tracked file plus untracked-but-not-ignored ones (so a secret
cannot slip in before `git add`) for the old default password and obvious credential patterns."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Built from pieces so this file does not contain the strings it hunts for.
OLD_DB_PASSWORD = "qs_dev" + "_password"
OLD_CI_PASSWORD = "qs_ci" + "_password"
OLD_JWT_DEFAULT = "dev-only-jwt" + "-secret"
OLD_PLACEHOLDER = "change_me" + "_locally"
OLD_E2E_PASSWORD = "an-e2e-test" + "-passphrase"
# The local dev admin password lives only in the git-ignored infra/.env, never in a tracked file.
DEV_ADMIN_PASSWORD = "admin" + "123"

FORBIDDEN_LITERALS = [
    OLD_DB_PASSWORD,
    OLD_CI_PASSWORD,
    OLD_JWT_DEFAULT,
    OLD_PLACEHOLDER,
    OLD_E2E_PASSWORD,
    DEV_ADMIN_PASSWORD,
]

PATTERNS = {
    "AWS access key id": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "API key (sk-...)": re.compile(r"\bsk-(?:or-v1-|ant-)?[A-Za-z0-9_-]{24,}"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
}
# user:password@host inside a URL, where the password is a literal (not a <placeholder>, ${VAR}, {fstring} or env ref).
URL_PASSWORD = re.compile(r"[a-z+]+://[^/\s:@'\"`<>{}$]+:([^@\s'\"`/<>{}$]+)@")

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".xlsx", ".docx", ".mp4", ".woff", ".woff2", ".lock"}
SKIP_NAMES = {"package-lock.json", "uv.lock", "test_secrets_guard.py"}


def _git_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=REPO,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available / not a git checkout")
    files = []
    for rel in filter(None, out.split("\0")):
        p = REPO / rel
        skip = p.suffix.lower() in SKIP_SUFFIXES or p.name in SKIP_NAMES
        if p.is_file() and not skip and p.stat().st_size < 2_000_000:
            files.append(p)
    return files


def _lines(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    yield from enumerate(text.splitlines(), 1)


def test_old_default_credentials_are_gone() -> None:
    hits = [
        f"{p.relative_to(REPO)}:{n}"
        for p in _git_files()
        for n, line in _lines(p)
        if any(lit in line for lit in FORBIDDEN_LITERALS)
    ]
    assert not hits, "old default secrets are still in the repo: " + ", ".join(hits)


def test_no_obvious_secret_patterns_in_the_repo() -> None:
    hits = []
    for p in _git_files():
        for n, line in _lines(p):
            for label, rx in PATTERNS.items():
                if rx.search(line):
                    hits.append(f"{p.relative_to(REPO)}:{n} ({label})")
            for m in URL_PASSWORD.finditer(line):
                if m.group(1).lower() not in {"pass", "password", "pw", "secret", "xxx", "xxxx"}:
                    hits.append(f"{p.relative_to(REPO)}:{n} (password in URL)")
    assert not hits, "possible committed secrets: " + ", ".join(hits)


def test_env_files_are_not_committed_except_the_example() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=False, timeout=60
    ).stdout.decode()
    env_re = re.compile(r"(^|/)\.env(\.[\w-]+)?$")
    envs = [f for f in tracked.split("\0") if f and env_re.search(f) and not f.endswith(".example")]
    assert envs == []


def test_compose_requires_secrets_instead_of_defaulting_them() -> None:
    compose = (REPO / "infra" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "${POSTGRES_PASSWORD:?" in compose and "${JWT_SECRET:?" in compose
    assert not re.search(r"\$\{(POSTGRES_PASSWORD|JWT_SECRET|ADMIN_PASSWORD|BOOTSTRAP_TOKEN):-[^}]", compose)


def test_infra_env_is_git_ignored() -> None:
    """infra/.env holds the real dev credentials (including the simple dev admin login); it must never be tracked."""
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "infra/.env"], cwd=REPO, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available / not a git checkout")
    assert r.returncode == 0, "infra/.env is not git-ignored"
