#!/usr/bin/env python3
"""End-to-end check of the chat scorecard builder against a RUNNING API (docker compose up).

    python scripts/e2e_chat_check.py                       # all tests against http://localhost:8000
    python scripts/e2e_chat_check.py --test 1              # only the user-specified-KPI test
    python scripts/e2e_chat_check.py --test 2 --keep       # only the open-ended tests, keep sessions
    QS_PASSWORD=... python scripts/e2e_chat_check.py --base-url http://localhost:8000 --email you@example.com

Stdlib only (no pip install). Makes REAL model calls (Bedrock, web search, Jev) through the API, so
it costs money and takes minutes; nothing secret is read or printed (the API holds the credentials).

Auth: signs in with --email / $QS_EMAIL and the password in $QS_PASSWORD (or a hidden prompt), then sends the
short-lived bearer token from POST /api/v1/auth/login. (Set a password for an existing account with
`python -m app.scripts.set_password <email>`.)

Flow per turn (backend/app/api/v1/chat.py): POST returns 202 immediately with turn_in_progress=true;
this script then polls GET /chat/sessions/{id} (state: draft, assistant_message, turn_in_progress,
turn_error) and GET /chat/sessions/{id}/turn-events (live trace) until the turn finishes.

Test 1  user-specified KPIs: a use case + 35 random distinct KPIs + "why is this scorecard needed?".
        Asserts all 35 names are in the draft EXACTLY (and nothing else was invented), leaf weights
        sum to 100, every leaf has all 11 (0-10) guidelines, name/purpose/domain/audience/target_score
        are filled, the invention fan-out did NOT run, and the message answers the side question.
Test 2  open-ended: two realistic prompts with no KPI list. Asserts a complete valid draft
        (categories, a reasonable KPI count, 11 guidelines per leaf with quantitative criteria, weights
        = 100) and that the research fan-out ran; REPORTS whether web search returned any results.

Exit code 0 only if every FAIL-level check passed. WARN lines are informational heuristics.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

POLL_SECONDS = 2.0
LEVELS = [str(i) for i in range(11)]

# A pool of realistic, distinct KPI names; test 1 samples 35 of them at random.
KPI_POOL = [
    "First Response Time", "Average Resolution Time", "First Contact Resolution Rate",
    "Customer Satisfaction Score", "Net Promoter Score", "Customer Effort Score",
    "Ticket Reopen Rate", "Backlog Age", "SLA Compliance Rate", "Escalation Rate",
    "Agent Utilization", "Knowledge Base Article Usage", "Self-Service Deflection Rate",
    "QA Audit Score", "Empathy and Tone Rating", "Script Adherence", "Call Abandonment Rate",
    "Average Handle Time", "Chat Concurrency Efficiency", "Email Response Accuracy",
    "Cost per Ticket", "Repeat Contact Rate", "Agent Onboarding Time", "Training Completion Rate",
    "Agent Attrition Rate", "Schedule Adherence", "Peak Hour Coverage", "Complaint Rate",
    "Compliance Violation Count", "Data Privacy Incident Rate", "Tool Downtime Impact",
    "Callback Success Rate", "Customer Churn After Contact", "Upsell Conversion Rate",
    "Documentation Quality Score", "Handoff Quality", "Root Cause Tagging Accuracy",
    "Proactive Outreach Rate", "Language Coverage", "Accessibility Support Rate",
    "Bug Report Quality", "Feedback Loop Closure Time", "Surveys Response Rate",
]

OPEN_ENDED_PROMPTS = [
    "I need a quality scorecard for evaluating incident response quality for our SRE on-call team "
    "at a cloud infrastructure company.",
    "Create a quality scorecard for evaluating outbound sales call quality for a B2B SaaS "
    "inside-sales team.",
]


# --- tiny HTTP layer -----------------------------------------------------------------------------


class Api:
    def __init__(self, base_url: str, email: str | None, password: str | None) -> None:
        self.base = base_url.rstrip("/")
        self.email, self.password = email, password
        self.token: str | None = None
        self.user_id: str | None = None

    def login(self) -> bool:
        status, body = self.call("POST", "/api/v1/auth/login", {"email": self.email, "password": self.password}, auth=False)
        if status != 200:
            return False
        self.token = body["access_token"]
        self.user_id = body["user"]["id"]
        return True

    def call(self, method: str, path: str, body: Any = None, *, auth: bool = True, timeout: float = 60.0):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, {"detail": raw.decode(errors="replace")[:300]}


# --- reporting -----------------------------------------------------------------------------------


class Report:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        self.passed += ok
        self.failed += not ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
        return ok

    @staticmethod
    def warn(label: str, detail: str = "") -> None:
        print(f"  [WARN] {label}" + (f" -- {detail}" if detail else ""))

    @staticmethod
    def info(label: str, detail: str = "") -> None:
        print(f"  [INFO] {label}" + (f" -- {detail}" if detail else ""))


# --- running one turn ----------------------------------------------------------------------------


class TurnResult:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.events: dict[str, dict[str, Any]] = {}
        self.seconds = 0.0
        self.error: str | None = None  # transport / timeout problem (not a turn_error)


def _print_new_events(seen: dict[str, dict[str, Any]], fresh: list[dict[str, Any]]) -> None:
    for ev in fresh:
        if ev["id"] not in seen:
            seen[ev["id"]] = ev
            print(f"      . [{ev['actor']}] {ev['event_type']}: {str(ev['message'])[:140]}")


def run_turn(api: Api, session_id: str, first_message: str | None, follow_up: str | None, timeout: float) -> TurnResult:
    """POST the first message (new session) or a follow-up, then poll until the turn finishes."""
    result = TurnResult()
    started = time.monotonic()
    if first_message is not None:
        status, body = api.call(
            "POST", "/api/v1/chat/sessions", {"message": first_message, "session_id": session_id}
        )
    else:
        status, body = api.call("POST", f"/api/v1/chat/sessions/{session_id}/messages", {"message": follow_up})
    if status != 202:
        result.error = f"POST expected 202 (background turn), got {status}: {body}"
        return result
    session_id = body["session_id"]
    deadline = started + timeout
    consecutive_errors = 0
    while time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
        status, state = api.call("GET", f"/api/v1/chat/sessions/{session_id}")
        if status != 200:
            consecutive_errors += 1
            if consecutive_errors >= 5:
                result.error = f"GET session failed {consecutive_errors}x: {status} {state}"
                return result
            continue
        consecutive_errors = 0
        ev_status, events = api.call("GET", f"/api/v1/chat/sessions/{session_id}/turn-events")
        if ev_status == 200 and isinstance(events, list):
            _print_new_events(result.events, events)
        if not state.get("turn_in_progress"):
            result.state = state
            result.seconds = time.monotonic() - started
            return result
    result.error = f"turn did not finish within {timeout:.0f}s"
    return result


# --- draft helpers -------------------------------------------------------------------------------


def leaves(draft: dict[str, Any]) -> list[dict[str, Any]]:
    kpis = draft.get("kpis") or []
    parents = {k.get("parent_name") for k in kpis if k.get("parent_name")}
    return [k for k in kpis if k["name"] not in parents]


def scored_leaves(draft: dict[str, Any]) -> list[dict[str, Any]]:
    return [k for k in leaves(draft) if k.get("included_in_scoring", True)]


def has_all_guidelines(kpi: dict[str, Any]) -> bool:
    g = kpi.get("guidelines") or {}
    return set(g) == set(LEVELS) and all(str((g[k] or {}).get("qualitative_text") or "").strip() for k in LEVELS)


def has_quantitative(kpi: dict[str, Any]) -> bool:
    g = kpi.get("guidelines") or {}
    return sum(1 for k in LEVELS if (g.get(k) or {}).get("quantitative_criteria")) >= 6


def summarize_events(events: dict[str, dict[str, Any]]) -> None:
    by_type: dict[str, int] = {}
    actors: set[str] = set()
    for ev in events.values():
        by_type[ev["event_type"]] = by_type.get(ev["event_type"], 0) + 1
        actors.add(ev["actor"])
    print(f"  trace: {len(events)} events, actors={sorted(actors)}")
    print("  trace by type: " + ", ".join(f"{k}={v}" for k, v in sorted(by_type.items())))


def web_search_summary(events: dict[str, dict[str, Any]]) -> tuple[int, int]:
    """(search calls, total results returned) parsed from 'Found N result(s) for ...' events."""
    calls = results = 0
    for ev in events.values():
        if ev["event_type"] == "search_result":
            calls += 1
            msg = str(ev["message"])
            try:
                results += int(msg.split("Found ", 1)[1].split(" ", 1)[0])
            except (IndexError, ValueError):
                pass
    return calls, results


def wait_for_session_error(state: dict[str, Any]) -> str | None:
    return state.get("turn_error") and f"{state.get('turn_error_code')}: {state['turn_error']}"


# --- test 1 --------------------------------------------------------------------------------------


def test_user_specified(api: Api, report: Report, timeout: float, keep: bool) -> None:
    print("\n=== TEST 1: user-specified KPIs (35 KPIs + side question) ===")
    rng = random.Random()
    names = rng.sample(KPI_POOL, 35)
    prompt = (
        "I'm setting up a quality scorecard for our customer support organization. "
        "Here are my KPIs, please use exactly these:\n"
        + "\n".join(f"- {n}" for n in names)
        + "\n\nI need 35 KPIs. Also, why is this scorecard needed in the first place?"
    )
    session_id = str(uuid.uuid4())
    res = run_turn(api, session_id, prompt, None, timeout)
    if not report.check(res.error is None, "turn completed (202 -> polled to done)", res.error or f"{res.seconds:.1f}s"):  # noqa: E501
        return
    state = res.state
    if not report.check(not wait_for_session_error(state), "no turn_error", str(wait_for_session_error(state) or "")):
        return
    draft = state.get("draft") or {}
    kpis = draft.get("kpis") or []
    got = [k["name"] for k in kpis]
    missing = [n for n in names if n not in got]
    extra = [n for n in got if n not in names]
    report.check(not missing, "all 35 user KPI names present EXACTLY", f"missing={missing}" if missing else "")
    report.check(not extra, "no invented/renamed KPIs", f"extra={extra[:10]}" if extra else "")
    report.check(len(kpis) == len(set(got)), "no duplicate KPI names in draft")
    weights = [float(k.get("weight") or 0.0) for k in scored_leaves(draft)]
    report.check(abs(sum(weights) - 100.0) <= 0.05, "leaf weights sum to 100", f"sum={sum(weights):.2f}")
    report.check(all(w > 0 for w in weights), "every scored leaf has a weight > 0")
    bad = [k["name"] for k in leaves(draft) if not has_all_guidelines(k)]
    report.check(not bad, "every leaf has all 11 guidelines (0-10) with qualitative text", f"bad={bad[:5]}" if bad else "")  # noqa: E501
    for field in ("name", "purpose", "domain", "audience"):
        report.check(bool(str(draft.get(field) or "").strip()), f"draft.{field} filled", str(draft.get(field))[:80])
    ts = draft.get("target_score")
    report.check(isinstance(ts, int | float) and 0 <= ts <= 10, "draft.target_score filled (0-10)", str(ts))
    if draft.get("scoring_formula"):
        report.info("optional scoring_formula set", str(draft["scoring_formula"])[:100])
    msgs = [str(ev["message"]) for ev in res.events.values()]
    report.check(
        not any(ev["event_type"] == "deciding_categories" for ev in res.events.values()),
        "invention fan-out did NOT run (no category decision)",
    )
    report.check(any("Detected your KPI list" in m for m in msgs), "trace shows user-specified mode detected")
    message = str(state.get("assistant_message") or "")
    print("\n  assistant message:\n    " + (message[:1500].replace("\n", "\n    ") or "(empty)"))
    report.check(bool(message.strip()), "assistant message present")
    answered = any(w in message.lower() for w in ("because", "needed", "purpose", "so that", "helps", "allows", "enables"))  # noqa: E501
    if answered:
        report.check(True, "message appears to ANSWER 'why is this scorecard needed' (keyword heuristic)")
    else:
        report.warn("could not detect an answer to the side question in the message -- read it above")
    summarize_events(res.events)
    report.info("status", f"{state.get('status')} in {res.seconds:.1f}s, kpis={len(kpis)}")
    if not keep:
        api.call("DELETE", f"/api/v1/chat/sessions/{session_id}")


# --- test 2 --------------------------------------------------------------------------------------


def test_open_ended(api: Api, report: Report, timeout: float, keep: bool, prompt: str, n: int) -> None:
    print(f"\n=== TEST 2.{n}: open-ended: {prompt[:80]}... ===")
    session_id = str(uuid.uuid4())
    res = run_turn(api, session_id, prompt, None, timeout)
    if not report.check(res.error is None, "first turn completed", res.error or f"{res.seconds:.1f}s"):
        return
    all_events = dict(res.events)
    state = res.state
    total = res.seconds
    # The assistant may pause to ask a clarifying/confirmation question; nudge it forward (max 2x).
    for nudge in range(2):
        draft = state.get("draft") or {}
        if state.get("turn_error") or state.get("status") == "confirmed":
            break
        if draft.get("kpis") and not _draft_incomplete(draft):
            break
        report.info(f"draft not complete yet (status={state.get('status')}); sending nudge {nudge + 1}")
        nxt = run_turn(
            api, session_id, None,
            "Please use your best judgement for anything still open and finalize the full draft "
            "(all KPIs with 0-10 guidelines and weights summing to 100).",
            timeout,
        )
        if nxt.error:
            report.check(False, "nudge turn completed", nxt.error)
            break
        all_events.update(nxt.events)
        state, total = nxt.state, total + nxt.seconds
    if not report.check(not wait_for_session_error(state), "no turn_error", str(wait_for_session_error(state) or "")):
        return
    draft = state.get("draft") or {}
    kpis = draft.get("kpis") or []
    cats = {k.get("parent_name") for k in kpis if k.get("parent_name")}
    lv = leaves(draft)
    report.check(len(cats) >= 2, "draft has KPI categories (>= 2)", f"{len(cats)} categories")
    report.check(8 <= len(lv) <= 80, "reasonable KPI count (8..80 leaves)", f"{len(lv)} leaves")
    weights = [float(k.get("weight") or 0.0) for k in scored_leaves(draft)]
    report.check(abs(sum(weights) - 100.0) <= 0.05, "leaf weights sum to 100", f"sum={sum(weights):.2f}")
    bad = [k["name"] for k in lv if not has_all_guidelines(k)]
    report.check(not bad, "every leaf has all 11 guidelines", f"bad={bad[:5]}" if bad else "")
    quant = sum(1 for k in lv if has_quantitative(k))
    report.check(
        lv != [] and quant >= 0.7 * len(lv),
        "guidelines carry quantitative criteria (>= 70% of leaves, >= 6 levels each)",
        f"{quant}/{len(lv)} leaves ({(100 * quant / len(lv)) if lv else 0:.0f}%)",
    )
    marked = sum(1 for k in lv if "[proposed target" in str((k.get("guidelines") or {}).get("10", {})))
    if marked:
        report.info("leaves marked '[proposed target - please validate]' (no numeric thresholds)", str(marked))
    for field in ("name", "purpose", "domain"):
        report.check(bool(str(draft.get(field) or "").strip()), f"draft.{field} filled")
    # audience is OPTIONAL for an open-ended scorecard (never blocks completion) -> WARN, not FAIL
    if str(draft.get("audience") or "").strip():
        report.info("draft.audience filled", str(draft.get("audience"))[:80])
    else:
        report.warn("draft.audience empty (optional in open-ended mode)")
    ts = draft.get("target_score")
    report.check(isinstance(ts, int | float) and 0 <= ts <= 10, "draft.target_score filled (0-10)", str(ts))
    types = {ev["event_type"] for ev in all_events.values()}
    actors = {ev["actor"] for ev in all_events.values()}
    report.check("deciding_categories" in types, "category decision ran (open-ended pipeline)")
    report.check(any(a.startswith("research_agent_") for a in actors), "research agents ran", f"{len(actors)} actors")
    calls, results = web_search_summary(all_events)
    report.check(calls > 0, "web search was invoked (trace has search events)", f"{calls} searches")
    if calls and results == 0:
        report.warn("web search returned ZERO results across all searches (gateway unconfigured/failing?)")
    else:
        report.info("web search results returned", f"{results} results over {calls} searches")
    print("\n  assistant message:\n    " + (str(state.get("assistant_message") or "")[:1200].replace("\n", "\n    ") or "(empty)"))  # noqa: E501
    summarize_events(all_events)
    report.info("done", f"status={state.get('status')} total={total:.1f}s categories={sorted(c for c in cats if c)[:8]}")  # noqa: E501
    if not keep:
        api.call("DELETE", f"/api/v1/chat/sessions/{session_id}")


def _draft_incomplete(draft: dict[str, Any]) -> bool:
    sl = scored_leaves(draft)
    return (
        not all(draft.get(f) for f in ("name", "purpose", "domain"))
        or draft.get("target_score") is None
        or abs(sum(float(k.get("weight") or 0.0) for k in sl) - 100.0) > 0.05
        or any(not k.get("guidelines") for k in leaves(draft))
    )


# --- main ----------------------------------------------------------------------------------------


def _make_output_encoding_safe() -> None:
    """Windows consoles default to cp1252, which cannot print the arrows/comparison signs in the report."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - not a TextIOWrapper (redirected/odd stream): leave it
            pass


def main() -> int:
    _make_output_encoding_safe()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("QS_API_URL", "http://localhost:8000"))
    parser.add_argument("--email", default=os.environ.get("QS_EMAIL"))
    parser.add_argument("--test", choices=["1", "2", "all"], default="all")
    parser.add_argument("--timeout", type=float, default=1800.0, help="seconds to wait per turn")
    parser.add_argument("--keep", action="store_true", help="do not delete the created chat sessions")
    args = parser.parse_args()

    if not args.email:
        print("Pass --email (or set $QS_EMAIL); the password comes from $QS_PASSWORD or a hidden prompt.")
        return 2
    import getpass

    api = Api(args.base_url, args.email, os.environ.get("QS_PASSWORD") or getpass.getpass(f"Password for {args.email}: "))
    status, health = api.call("GET", "/health", auth=False, timeout=10)
    if status != 200:
        print(f"API not healthy at {args.base_url}: {status} {health}")
        return 2
    if not api.login():
        print("Sign-in failed: check --email / the password (and that the account has one: app.scripts.set_password).")
        return 2
    print(f"API {args.base_url} ok; acting as user {api.user_id}")

    report = Report()
    t0 = time.monotonic()
    if args.test in ("1", "all"):
        test_user_specified(api, report, args.timeout, args.keep)
    if args.test in ("2", "all"):
        for i, prompt in enumerate(OPEN_ENDED_PROMPTS, start=1):
            test_open_ended(api, report, args.timeout, args.keep, prompt, i)
    print(f"\nSUMMARY: {report.passed} passed, {report.failed} failed in {time.monotonic() - t0:.0f}s")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
