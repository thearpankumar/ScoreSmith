"""The chat model picker (app/ai/chat_models.py, `model` on chat messages, GET /chat/config) and chat attachments
(app/api/v1/chat_attachments.py, app/ai/attachments.py): through the real app and Postgres with fake LLM / AWS
clients, plus unit tests of the extraction and prompt budgeting."""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.ai import attachments as att
from app.ai import providers
from app.config import get_settings
from app.deps import get_aws_jobs, get_bedrock_client, get_jev_client, get_web_search_client
from app.main import app
from tests.fakes import FakeAwsJobs, FakeBedrockClient, FakeJevClient, FakeOpenRouterClient, text_result

OPUS = "anthropic/claude-opus-5.5"
MINI = "openai/gpt-5-mini"


@pytest.fixture(autouse=True)
def _overrides(monkeypatch):
    aws = FakeAwsJobs()
    app.dependency_overrides[get_web_search_client] = lambda: None
    app.dependency_overrides[get_jev_client] = lambda: FakeJevClient()
    app.dependency_overrides[get_aws_jobs] = lambda: aws
    monkeypatch.setattr(get_settings(), "s3_bucket", "test-bucket")
    yield aws
    for dep in (get_bedrock_client, get_web_search_client, get_jev_client, get_aws_jobs):
        app.dependency_overrides.pop(dep, None)


@pytest.fixture()
def openrouter(monkeypatch):
    async def active() -> str:
        return "openrouter"

    monkeypatch.setattr(providers, "active_provider", active)


def _user(client: TestClient, seed_user_id: str) -> dict:
    return client.post(
        "/api/v1/users", json={"email": f"u-{uuid.uuid4().hex[:8]}@example.com", "name": "U"},
        headers={"X-User-Id": seed_user_id},
    ).json()


def _title_fn(**kw):
    return text_result("A Title")


# --- GET /chat/config -----------------------------------------------------------------------------------------


def test_config_on_bedrock_offers_no_picker_and_no_attachments(client: TestClient, seed_user_id: str) -> None:
    user = _user(client, seed_user_id)
    body = client.get("/api/v1/chat/config", headers={"X-User-Id": user["id"]}).json()
    assert body["provider"] == "bedrock" and body["model_picker"] is False and body["models"] == []
    assert body["attachments"]["enabled"] is False


def test_config_on_openrouter_lists_the_enabled_models(client: TestClient, seed_user_id: str, openrouter) -> None:
    user = _user(client, seed_user_id)
    body = client.get("/api/v1/chat/config", headers={"X-User-Id": user["id"]}).json()
    assert body["model_picker"] is True and body["default_model"] == MINI
    labels = [m["label"] for m in body["models"]]
    assert labels == ["GPT-5 mini", "Claude Sonnet 5.5", "Claude Opus 5.5", "GPT-5.6 Luna"]
    assert body["attachments"]["enabled"] is True and "xlsx" in body["attachments"]["extensions"]


# --- the picked model -------------------------------------------------------------------------------------------


def test_the_picked_model_runs_the_assistant_and_is_remembered(
    client: TestClient, seed_user_id: str, openrouter
) -> None:
    user = _user(client, seed_user_id)
    replies = iter([text_result("Hello from Opus."), text_result("Still Opus.")])
    fake = FakeOpenRouterClient(converse_fn=_title_fn, agent_fn=lambda **_: next(replies))
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    headers = {"X-User-Id": user["id"]}
    first = client.post("/api/v1/chat/sessions", json={"message": "hi", "model": OPUS}, headers=headers)
    assert first.status_code == 201, first.text
    sid = first.json()["session_id"]
    # The next message names no model: the session's pick is kept.
    second = client.post(f"/api/v1/chat/sessions/{sid}/messages", json={"message": "and?"}, headers=headers)
    assert second.status_code == 200, second.text
    assert [c["model_id"] for c in fake.agent_calls] == [OPUS, OPUS]
    assert client.get(f"/api/v1/chat/sessions/{sid}", headers=headers).json()["chat_model"] == OPUS
    messages = client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=headers).json()
    assert [m["model"] for m in messages if m["role"] == "assistant"] == [OPUS, OPUS]


def test_a_model_the_admin_did_not_enable_falls_back_to_the_default(
    client: TestClient, seed_user_id: str, openrouter, monkeypatch
) -> None:
    from app.ai import chat_models

    async def stored():
        return {"enabled": [MINI], "default": MINI}

    monkeypatch.setattr(chat_models, "stored_value", stored)
    user = _user(client, seed_user_id)
    fake = FakeOpenRouterClient(converse_fn=_title_fn, agent_fn=lambda **_: text_result("ok"))
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    r = client.post("/api/v1/chat/sessions", json={"message": "hi", "model": OPUS}, headers={"X-User-Id": user["id"]})
    assert r.status_code == 201, r.text
    assert fake.agent_calls[0]["model_id"] == MINI


def test_a_builder_reply_is_labelled_with_the_builder_model(
    client: TestClient, seed_user_id: str, openrouter
) -> None:
    from tests.fakes import tool_use_result

    user = _user(client, seed_user_id)

    def builder(*, tools=None, **_):
        if not tools:
            return text_result("A Title")
        return tool_use_result("ask_clarification", {"question": "Who?", "options": ["A", "B"], "missing_fields": []})

    fake = FakeOpenRouterClient(converse_fn=builder)  # the default assistant hands every message to the builder
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    headers = {"X-User-Id": user["id"]}
    sid = client.post("/api/v1/chat/sessions", json={"message": "build one", "model": OPUS}, headers=headers).json()[
        "session_id"
    ]
    messages = client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=headers).json()
    assert messages[-1]["model"] == "openai/gpt-5.6-luna"


# --- attachments API --------------------------------------------------------------------------------------------


def _xlsx(rows: list[list]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "KPIs"
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client: TestClient, aws: FakeAwsJobs, headers: dict, name: str, data: bytes) -> dict:
    init = client.post("/api/v1/chat/attachments", json={"filename": name, "size": len(data)}, headers=headers)
    assert init.status_code == 201, init.text
    body = init.json()
    key = f"chat/{headers['X-User-Id']}/{body['attachment']['id']}.{name.rsplit('.', 1)[-1]}"
    assert body["parts"][0]["url"].startswith(f"https://s3.fake/{key}")
    aws.put_object(key, data)
    done = client.post(
        f"/api/v1/chat/attachments/{body['attachment']['id']}/complete",
        json={"upload_id": body["upload_id"], "parts": [{"part_number": 1, "etag": "e1"}]},
        headers=headers,
    )
    assert done.status_code == 200, done.text
    return done.json()


def test_attachments_need_openrouter(client: TestClient, seed_user_id: str) -> None:
    user = _user(client, seed_user_id)
    r = client.post(
        "/api/v1/chat/attachments", json={"filename": "a.pdf", "size": 10}, headers={"X-User-Id": user["id"]}
    )
    assert r.status_code == 409


def test_unsupported_and_oversized_files_are_refused(client: TestClient, seed_user_id: str, openrouter) -> None:
    headers = {"X-User-Id": _user(client, seed_user_id)["id"]}
    assert client.post("/api/v1/chat/attachments", json={"filename": "a.exe", "size": 10}, headers=headers
                       ).status_code == 422
    too_big = get_settings().chat_attachment_max_bytes + 1
    assert client.post("/api/v1/chat/attachments", json={"filename": "a.pdf", "size": too_big}, headers=headers
                       ).status_code == 422


def test_a_spreadsheet_is_profiled_and_sent_to_the_models(
    client: TestClient, seed_user_id: str, openrouter, _overrides
) -> None:
    aws = _overrides
    headers = {"X-User-Id": _user(client, seed_user_id)["id"]}
    data = _xlsx([["KPI", "Weight", "Since"], ["First Call Resolution", 40, date(2026, 1, 1)],
                  ["Empathy", 60, date(2026, 2, 1)]])
    ready = _upload(client, aws, headers, "kpis.xlsx", data)
    assert ready["status"] == "ready" and ready["outline"]["sheets"][0]["rows"] == 2

    seen: dict = {}

    def agent_fn(*, messages, system, tools, **_):
        seen["text"] = messages[-1]["content"][0]["text"]
        seen["system"], seen["tools"] = system, {t.name for t in tools}
        return text_result("Two KPIs: First Call Resolution (40%) and Empathy (60%).")

    fake = FakeOpenRouterClient(converse_fn=_title_fn, agent_fn=agent_fn)
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    r = client.post(
        "/api/v1/chat/sessions",
        json={"message": "what's in this file?", "attachment_ids": [ready["id"]]}, headers=headers,
    )
    assert r.status_code == 201, r.text
    assert "<attached_files>" in seen["text"] and "- First Call Resolution | 40 | 2026-01-01" in seen["text"]
    assert "kpis.xlsx" in seen["system"] and "read_attachment" in seen["tools"]
    sid = r.json()["session_id"]
    messages = client.get(f"/api/v1/chat/sessions/{sid}/messages", headers=headers).json()
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["content"] == "what's in this file?"  # the transcript keeps the user's own words
    assert [a["filename"] for a in user_message["attachments"]] == ["kpis.xlsx"]
    # Sent files can no longer be deleted.
    assert client.delete(f"/api/v1/chat/attachments/{ready['id']}", headers=headers).status_code == 409


def test_a_pdf_is_read_by_the_extraction_lambda(client: TestClient, seed_user_id: str, openrouter, _overrides) -> None:
    aws = _overrides
    aws.invoke_fn = lambda name, payload: {
        "ok": True, "kind": "pdf", "text": "Step 1: greet the caller.", "pages": 3, "chars": 25, "truncated": False,
        "scanned_pages": [], "needs_native": False,
    }
    headers = {"X-User-Id": _user(client, seed_user_id)["id"]}
    ready = _upload(client, aws, headers, "sop.pdf", b"%PDF-1.7 fake")
    assert ready["status"] == "ready" and ready["outline"]["pages"] == 3
    name, payload = aws.invocations[0]
    assert name == get_settings().chat_extract_function
    assert payload["mode"] == "chat_extract" and payload["kind"] == "pdf" and payload["key"].startswith("chat/")


def test_a_failed_extraction_and_a_wrong_signature_are_reported(
    client: TestClient, seed_user_id: str, openrouter, _overrides
) -> None:
    aws = _overrides
    aws.invoke_fn = lambda name, payload: {"ok": False, "error": "password-protected PDF"}
    headers = {"X-User-Id": _user(client, seed_user_id)["id"]}
    failed = _upload(client, aws, headers, "locked.pdf", b"%PDF-1.7")
    assert failed["status"] == "failed" and "password-protected" in failed["error"]
    fake_png = _upload(client, aws, headers, "photo.png", b"not really a png")
    assert fake_png["status"] == "failed" and "does not look like" in fake_png["error"]


def test_another_users_attachment_cannot_be_sent(client: TestClient, seed_user_id: str, openrouter, _overrides) -> None:
    aws = _overrides
    owner = {"X-User-Id": _user(client, seed_user_id)["id"]}
    other = {"X-User-Id": _user(client, seed_user_id)["id"]}
    ready = _upload(client, aws, owner, "notes.txt", b"hello")
    app.dependency_overrides[get_bedrock_client] = lambda: FakeOpenRouterClient(converse_fn=_title_fn)
    r = client.post(
        "/api/v1/chat/sessions", json={"message": "x", "attachment_ids": [ready["id"]]}, headers=other
    )
    assert r.status_code == 422
    assert client.delete(f"/api/v1/chat/attachments/{ready['id']}", headers=other).status_code == 404


def test_an_image_is_described_once_and_sent_natively(
    client: TestClient, seed_user_id: str, openrouter, _overrides
) -> None:
    aws = _overrides
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    described: list = []

    def converse_fn(*, messages, model_id=None, **_):
        blocks = messages[0]["content"]
        if any("image" in b for b in blocks):
            described.append(model_id)
            return text_result("A dashboard showing CSAT 92%.")
        return text_result("A Title")

    seen: dict = {}

    def agent_fn(*, messages, **_):
        seen["blocks"] = messages[-1]["content"]
        return text_result("It shows CSAT at 92%.")

    fake = FakeOpenRouterClient(converse_fn=converse_fn, agent_fn=agent_fn)
    app.dependency_overrides[get_bedrock_client] = lambda: fake
    headers = {"X-User-Id": _user(client, seed_user_id)["id"]}
    ready = _upload(client, aws, headers, "dash.png", png)
    assert ready["status"] == "ready" and described == ["openai/gpt-5.6-luna"]
    r = client.post(
        "/api/v1/chat/sessions", json={"message": "what does it show?", "attachment_ids": [ready["id"]]},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    assert "CSAT 92%" in seen["blocks"][0]["text"]
    assert any(b.get("image", {}).get("source", {}).get("bytes") == png for b in seen["blocks"])


# --- extraction and budgeting (units) ----------------------------------------------------------------------------


def test_large_sheets_keep_their_first_and_last_rows_with_a_note() -> None:
    rows = [["Name", "Score"]] + [[f"Agent {i}", i] for i in range(5000)]
    out = att.extract_spreadsheet(_xlsx(rows), budget_chars=4000)
    sheet = out.outline["sheets"][0]
    assert out.outline["truncated"] and sheet["rows"] == 5000 and sheet["omitted_rows"]
    assert "- Agent 0 | 0" in out.text and "- Agent 4999 | 4999" in out.text
    assert "omitted to fit" in out.text
    assert "- Score: number, min 0, max 4999; 5000 filled, 5000 distinct" in out.text


def test_csv_is_sniffed_and_profiled() -> None:
    out = att.extract_csv(b"kpi;target\nAHT;300\nCSAT;90\n")
    assert "Columns: kpi | target" in out.text and "- AHT | 300" in out.text
    assert "- target: number, min 90, max 300" in out.text


def test_render_block_shares_the_budget_and_compose_keeps_plain_messages() -> None:
    small = att.AttachmentView(id="a", filename="small.txt", kind="text", text="short")
    big = att.AttachmentView(id="b", filename="big.txt", kind="text", text="x" * 10_000)
    block = att.render_block([big, small], budget_tokens=1000)
    assert "short" in block and "more characters not included" in block
    assert len(block) < 4300
    assert att.compose_message("hello", []) == "hello"


def test_old_attachment_blocks_are_collapsed() -> None:
    block = att.render_block([att.AttachmentView(id="a", filename="sop.pdf", kind="pdf", text="body")], 100)
    messages = [
        {"role": "user", "content": f"first\n\n{block}"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    out = att.collapse_old_blocks(messages)
    assert out[0]["content"].startswith("first") and "body" not in out[0]["content"]
    assert "attached earlier: sop.pdf" in out[0]["content"]
    assert messages[0]["content"].endswith("</attached_files>")  # the input is not modified


def test_attachment_context_reads_on_from_an_offset() -> None:
    view = att.AttachmentView(id="a", filename="f.txt", kind="text", text="0123456789" * 5000)
    ctx = att.AttachmentContext(files=[view])
    first = ctx.read("a")
    assert "characters 0-30000 of 50000" in first and "read again with offset 30000" in first
    assert "characters 30000-50000" in ctx.read("a", 30000)
    assert "No attachment" in ctx.read("zzz")


def test_bedrock_client_is_never_given_native_blocks() -> None:
    assert providers.provider_of(FakeBedrockClient()) == "bedrock"
