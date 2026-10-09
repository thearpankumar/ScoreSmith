"""HTTP-level tests of the AI-evaluation endpoints (docs/ai-eval-contract.md) with fake AWS / Bedrock."""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy_utils import Ltree

from app.deps import get_aws_jobs, get_bedrock_client
from app.main import app
from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.models.scorecard import Scorecard
from app.models.scorecard_version import ScorecardVersion
from app.pipeline.dispatcher import Dispatcher, get_dispatcher
from tests.fakes import FakeAwsJobs, FakeBedrockClient, FakeJevScoreClient, master_converse_fn

MIB = 1024 * 1024
DRIVE = "https://drive.google.com/drive/folders/1AbC"


@pytest.fixture()
def aws():
    fake = FakeAwsJobs(hold=True)
    bedrock = FakeBedrockClient(converse_fn=master_converse_fn())
    dispatcher = Dispatcher(fake, bedrock, FakeJevScoreClient(), poll_seconds=0.01, patience_waits=())
    app.dependency_overrides[get_aws_jobs] = lambda: fake
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_bedrock_client] = lambda: FakeBedrockClient(converse_fn=master_converse_fn())
    yield fake
    for dep in (get_aws_jobs, get_dispatcher, get_bedrock_client):
        app.dependency_overrides.pop(dep, None)


def make_scorecard(db_session, owner_id: uuid.UUID) -> dict:
    """A scorecard (two leaf KPIs with 11 rubric levels, a current version) owned by `owner_id`."""
    sc = Scorecard(name="API AI Scorecard", owner_id=owner_id, domain="Hackathon")
    db_session.add(sc)
    db_session.flush()
    version = ScorecardVersion(scorecard_id=sc.id, version_number=1, created_by=owner_id)
    db_session.add(version)
    db_session.flush()
    sc.current_version_id = version.id
    ids = [uuid.uuid4(), uuid.uuid4()]
    for i, nid in enumerate(ids):
        db_session.add(KpiNode(id=nid, scorecard_version_id=version.id, parent_id=None, path=Ltree(nid.hex), level=1,
                               name=f"KPI {i}", weight=50, display_order=i))
    db_session.flush()
    for nid in ids:
        for level in range(11):
            db_session.add(KpiGuideline(kpi_node_id=nid, score_level=level, qualitative_text=f"L{level}"))
    db_session.commit()
    return {"id": str(sc.id), "version_id": str(version.id), "empty_id": _empty_scorecard(db_session, owner_id)}


@pytest.fixture()
def scorecard(db_session, seed_user_id):
    return make_scorecard(db_session, uuid.UUID(seed_user_id))


def _empty_scorecard(db_session, owner_id) -> str:
    sc = Scorecard(name="Empty", owner_id=owner_id)
    db_session.add(sc)
    db_session.commit()
    return str(sc.id)


def H(user_id: str) -> dict[str, str]:
    return {"X-User-Id": user_id}


def init_uploads(client, uid, files, purpose="submission"):
    return client.post("/api/v1/evaluations/ai/uploads", json={"purpose": purpose, "files": files}, headers=H(uid))


# --- uploads ------------------------------------------------------------------------------------------


def test_upload_init_returns_presigned_parts(client: TestClient, seed_user_id: str, aws) -> None:
    r = init_uploads(client, seed_user_id, [
        {"name": "demo.mp4", "size": 70 * MIB, "content_type": "video/mp4"},
        {"name": "Report.PDF", "size": 1000, "content_type": "application/pdf"},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["part_size"] == 32 * MIB
    f0, f1 = body["files"]
    assert [f0["client_index"], f1["client_index"]] == [0, 1]
    assert len(f0["parts"]) == 3 and [p["part_number"] for p in f0["parts"]] == [1, 2, 3] and len(f1["parts"]) == 1
    assert f0["s3_key"].startswith(f"uploads/{seed_user_id}/{body['upload_group_id']}/")
    assert f0["s3_key"].endswith(".mp4")
    assert f1["s3_key"].endswith(".pdf") and "expires=900" in f0["parts"][0]["url"]
    assert (f0["s3_key"], f0["upload_id"]) in aws.multiparts


def test_upload_init_batch_sheet_uses_batches_prefix(client: TestClient, seed_user_id: str, aws) -> None:
    r = init_uploads(client, seed_user_id, [{"name": "s.xlsx", "size": 5000}], purpose="batch_sheet")
    assert r.status_code == 200 and r.json()["files"][0]["s3_key"].startswith("batches/")


@pytest.mark.parametrize(
    ("purpose", "files"),
    [
        ("submission", [{"name": "evil.exe", "size": 10}]),
        ("submission", [{"name": "noext", "size": 10}]),
        ("submission", [{"name": "a.xlsx", "size": 10}]),
        ("submission", [{"name": "a.pdf", "size": 0}]),
        ("submission", [{"name": "a.mp4", "size": 2 * 1024**3 + 1}]),
        ("submission", [{"name": f"f{i}.pdf", "size": 10} for i in range(11)]),
        ("submission", []),
        ("batch_sheet", [{"name": "a.pdf", "size": 10}]),
        ("batch_sheet", [{"name": "a.csv", "size": 10}, {"name": "b.csv", "size": 10}]),
        ("batch_sheet", [{"name": "a.csv", "size": 30 * MIB}]),
        ("other", [{"name": "a.pdf", "size": 10}]),
    ],
)
def test_upload_init_validation_422(client: TestClient, seed_user_id: str, aws, purpose, files) -> None:
    r = init_uploads(client, seed_user_id, files, purpose=purpose)
    assert r.status_code == 422, r.text
    assert aws.multiparts == {}


def test_upload_exactly_two_gib_and_ten_files_are_accepted(client: TestClient, seed_user_id: str, aws) -> None:
    r = init_uploads(client, seed_user_id, [{"name": "big.mov", "size": 2 * 1024**3}])
    assert r.status_code == 200
    assert len(r.json()["files"][0]["parts"]) <= 10_000
    assert init_uploads(client, seed_user_id, [{"name": f"f{i}.pdf", "size": 5} for i in range(10)]).status_code == 200


def test_upload_requires_auth(client: TestClient, aws) -> None:
    assert client.post("/api/v1/evaluations/ai/uploads", json={"purpose": "submission", "files": []}).status_code == 401


def test_upload_complete_checks_size_and_magic_bytes(client: TestClient, seed_user_id: str, aws) -> None:
    init = init_uploads(client, seed_user_id, [{"name": "a.pdf", "size": 100}, {"name": "b.pdf", "size": 100},
                                               {"name": "c.mp4", "size": 100}]).json()
    good, bad, missing = init["files"]
    aws.put_object(good["s3_key"], b"%PDF-1.7 hello")
    aws.put_object(bad["s3_key"], b"MZ not a pdf at all")
    parts = [{"part_number": 1, "etag": "e1"}]
    body = {"files": [
        {"upload_id": good["upload_id"], "s3_key": good["s3_key"], "parts": parts},
        {"upload_id": bad["upload_id"], "s3_key": bad["s3_key"], "parts": parts},
        {"upload_id": missing["upload_id"], "s3_key": missing["s3_key"], "parts": parts},
        {"upload_id": "x", "s3_key": f"uploads/{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}.pdf", "parts": parts},
        {"upload_id": "x", "s3_key": "../../etc/passwd", "parts": parts},
    ]}
    r = client.post("/api/v1/evaluations/ai/uploads/complete", json=body, headers=H(seed_user_id))
    assert r.status_code == 200, r.text
    res = r.json()["files"]
    assert res[0]["ok"] is True and res[0]["size"] == len(b"%PDF-1.7 hello")
    assert res[1]["ok"] is False and "valid .pdf" in res[1]["error"] and bad["s3_key"] in aws.deleted
    assert res[2]["ok"] is False  # empty object (nothing uploaded)
    assert res[3]["ok"] is False and res[3]["error"] == "Invalid upload key."
    assert res[4]["ok"] is False


def test_upload_complete_rejects_empty_parts(client: TestClient, seed_user_id: str, aws) -> None:
    r = client.post("/api/v1/evaluations/ai/uploads/complete", headers=H(seed_user_id),
                    json={"files": [{"upload_id": "u", "s3_key": "k", "parts": []}]})
    assert r.status_code == 422


def test_upload_abort(client: TestClient, seed_user_id: str, aws) -> None:
    f = init_uploads(client, seed_user_id, [{"name": "a.pdf", "size": 100}]).json()["files"][0]
    r = client.post("/api/v1/evaluations/ai/uploads/abort", headers=H(seed_user_id), json={"files": [
        {"upload_id": f["upload_id"], "s3_key": f["s3_key"]},
        {"upload_id": "zzz", "s3_key": "uploads/not/ours.pdf"},
    ]})
    assert r.status_code == 204
    assert aws.aborted == [(f["s3_key"], f["upload_id"])]


# --- batch parse --------------------------------------------------------------------------------------


def _sheet() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["Timestamp", "Email Address", "Name", "Contact No", "Google Drive URL"])
    ws.append(["10/01/2026 09:00:00", "a@x.com", "Ann", "1", DRIVE])
    ws.append(["10/03/2026 09:00:00", "a@x.com", "Ann", "1", "https://drive.google.com/drive/folders/NEW"])
    ws.append(["10/01/2026 10:00:00", "b@x.com", "Bob", "2", "https://bad.example.com/x"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_batch_parse_returns_deduped_preview(client: TestClient, seed_user_id: str, aws) -> None:
    f = init_uploads(client, seed_user_id, [{"name": "s.xlsx", "size": 100}], purpose="batch_sheet").json()["files"][0]
    aws.put_object(f["s3_key"], _sheet())
    r = client.post("/api/v1/evaluations/ai/batches/parse", json={"s3_key": f["s3_key"]}, headers=H(seed_user_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert [row["email"] for row in body["rows"]] == ["a@x.com", "b@x.com"]
    assert body["rows"][0]["drive_url"].endswith("NEW") and body["rows"][1]["warnings"]
    assert body["skipped"][0]["row_index"] == 2
    assert body["columns"]["drive_url"] == "Google Drive URL"


def test_batch_parse_422s(client: TestClient, seed_user_id: str, aws) -> None:
    for key in ("uploads/x.xlsx", f"batches/{uuid.uuid4()}/{uuid.uuid4()}.pdf", "../etc"):
        r = client.post("/api/v1/evaluations/ai/batches/parse", json={"s3_key": key}, headers=H(seed_user_id))
        assert r.status_code == 422
    f = init_uploads(client, seed_user_id, [{"name": "s.xlsx", "size": 100}], purpose="batch_sheet").json()["files"][0]
    aws.put_object(f["s3_key"], b"garbage")
    r = client.post("/api/v1/evaluations/ai/batches/parse", json={"s3_key": f["s3_key"]}, headers=H(seed_user_id))
    assert r.status_code == 422 and "xlsx" in r.json()["detail"]


# --- jobs ---------------------------------------------------------------------------------------------


def post_jobs(client, uid, scorecard_id, items, direction=None):
    return client.post("/api/v1/evaluations/ai/jobs", headers=H(uid),
                       json={"scorecard_id": scorecard_id, "direction_prompt": direction, "items": items})


def test_single_job_creates_one_queued_evaluation(client: TestClient, seed_user_id: str, scorecard, aws) -> None:
    f = init_uploads(client, seed_user_id, [{"name": "a.pdf", "size": 100}]).json()["files"][0]
    r = post_jobs(client, seed_user_id, scorecard["id"], [{
        "name": None, "subject_email": "Jane@Example.com", "subject_name": "Jane",
        "sources": [{"kind": "upload", "s3_key": f["s3_key"], "original_name": "a.pdf", "size": 100},
                    {"kind": "drive", "drive_url": DRIVE}],
    }], direction="  focus on ML  ")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["batch_id"] is None and len(body["evaluations"]) == 1
    ev = body["evaluations"][0]
    assert ev["status"] == "queued" and ev["stage"] == "queued" and ev["source_kind"] == "mixed"
    assert ev["subject_email"] == "jane@example.com" and ev["direction_prompt"] == "focus on ML"
    assert ev["scorecard_version_id"] == scorecard["version_id"] and ev["queued_at"] and ev["batch_id"] is None
    assert ev["error_code"] is None and ev["started_at"] is None and ev["finished_at"] is None

    p = client.get(f"/api/v1/evaluations/{ev['id']}/progress", headers=H(seed_user_id))
    assert p.status_code == 200
    pb = p.json()
    assert pb["status"] == "queued" and pb["queue_position"] == 1 and pb["progress"] is None
    assert {s["kind"] for s in pb["sources"]} == {"upload", "drive"} and pb["sources"][0]["status"] == "pending"
    assert pb["events"][0]["event_type"] == "queued"

    # The existing evaluation endpoints return the added fields too (and /ai/... does not collide with /{id}).
    got = client.get(f"/api/v1/evaluations/{ev['id']}")
    assert got.status_code == 200 and got.json()["status"] == "queued" and got.json()["kpi_results"] == []
    listed = client.get("/api/v1/evaluations").json()
    assert listed[0]["stage"] == "queued" and "queued_at" in listed[0]


def test_batch_job_creates_batch_and_fifo_queue_positions(
    client: TestClient, seed_user_id: str, scorecard, aws
) -> None:
    items = [
        {"subject_email": f"p{i}@x.com", "sources": [{"kind": "drive", "drive_url": f"{DRIVE}{i}"}]}
        for i in range(3)
    ]
    r = post_jobs(client, seed_user_id, scorecard["id"], items)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["batch_id"] and len(body["evaluations"]) == 3
    positions = [client.get(f"/api/v1/evaluations/{e['id']}/progress", headers=H(seed_user_id)).json()["queue_position"]
                 for e in body["evaluations"]]
    assert positions == [1, 2, 3]
    b = client.get(f"/api/v1/evaluations/ai/batches/{body['batch_id']}", headers=H(seed_user_id))
    assert b.status_code == 200
    bb = b.json()
    assert bb["total"] == 3 and bb["counts"] == {"queued": 3, "running": 0, "completed": 0, "failed": 0}
    assert bb["scorecard_id"] == scorecard["id"] and bb["status"] == "queued" and len(bb["evaluations"]) == 3
    assert all(e["batch_id"] == body["batch_id"] for e in bb["evaluations"])


def test_job_validation_errors(client: TestClient, seed_user_id: str, scorecard, aws) -> None:
    other_user_key = f"uploads/{uuid.uuid4()}/{uuid.uuid4()}/{uuid.uuid4()}.pdf"
    r = post_jobs(client, seed_user_id, scorecard["id"], [
        {"sources": [{"kind": "drive", "drive_url": "https://evil.example.com/x"}]},
        {"sources": [{"kind": "drive", "drive_url": "http://drive.google.com/drive/folders/1"}]},
        {"sources": [{"kind": "upload", "s3_key": other_user_key}]},
        {"sources": [{"kind": "drive", "drive_url": DRIVE}]},
    ])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert sorted((d["item"], d["source"]) for d in detail) == [(0, 0), (1, 0), (2, 0)]
    # Nothing was created.
    assert client.get("/api/v1/evaluations").json() == []

    assert post_jobs(client, seed_user_id, scorecard["id"], []).status_code == 422
    assert post_jobs(client, seed_user_id, scorecard["id"], [{"sources": []}]).status_code == 422
    assert post_jobs(client, seed_user_id, scorecard["id"], [{"sources": [{"kind": "drive", "drive_url": DRIVE}]}],
                     direction="x" * 2001).status_code == 422
    assert post_jobs(client, seed_user_id, str(uuid.uuid4()), [{"sources": [{"kind": "drive", "drive_url": DRIVE}]}]
                     ).status_code == 404
    assert post_jobs(client, seed_user_id, scorecard["empty_id"], [{"sources": [{"kind": "drive", "drive_url": DRIVE}]}]
                     ).status_code == 422


def test_job_upload_key_must_belong_to_user(client: TestClient, db_session, seed_user_id: str, scorecard, aws) -> None:
    f = init_uploads(client, seed_user_id, [{"name": "a.pdf", "size": 100}]).json()["files"][0]
    ok = post_jobs(client, seed_user_id, scorecard["id"], [{"sources": [{"kind": "upload", "s3_key": f["s3_key"]}]}])
    assert ok.status_code == 202
    other = client.post("/api/v1/users", json={"email": "other@x.com", "name": "Other"}, headers=H(seed_user_id)).json()
    # The other user may not evaluate against this scorecard at all (404) ...
    items = [{"sources": [{"kind": "upload", "s3_key": f["s3_key"]}]}]
    foreign = post_jobs(client, other["id"], scorecard["id"], items)
    assert foreign.status_code == 404
    # ... and even against their OWN scorecard they cannot attach a file uploaded by someone else.
    mine = make_scorecard(db_session, uuid.UUID(other["id"]))
    stolen = post_jobs(client, other["id"], mine["id"], items)
    assert stolen.status_code == 422


# --- cancel / retry / not found ------------------------------------------------------------------------


def test_cancel_and_retry_endpoints(client: TestClient, seed_user_id: str, scorecard, aws) -> None:
    ev = post_jobs(client, seed_user_id, scorecard["id"], [{"sources": [{"kind": "drive", "drive_url": DRIVE}]}]
                   ).json()["evaluations"][0]
    eid = ev["id"]
    assert client.post(f"/api/v1/evaluations/{eid}/retry", headers=H(seed_user_id)).status_code == 409  # not failed

    r = client.post(f"/api/v1/evaluations/{eid}/cancel", headers=H(seed_user_id))
    assert r.status_code == 202, r.text
    assert r.json()["status"] == "failed" and r.json()["error_code"] == "cancelled" and r.json()["finished_at"]
    assert client.post(f"/api/v1/evaluations/{eid}/cancel", headers=H(seed_user_id)).status_code == 409

    p = client.get(f"/api/v1/evaluations/{eid}/progress", headers=H(seed_user_id)).json()
    assert p["status"] == "failed" and p["error_code"] == "cancelled" and p["queue_position"] is None

    r = client.post(f"/api/v1/evaluations/{eid}/retry", headers=H(seed_user_id))
    assert r.status_code == 202 and r.json()["status"] == "queued" and r.json()["attempt"] == 2
    assert r.json()["error_code"] is None

    missing = str(uuid.uuid4())
    assert client.post(f"/api/v1/evaluations/{missing}/cancel", headers=H(seed_user_id)).status_code == 404
    assert client.post(f"/api/v1/evaluations/{missing}/retry", headers=H(seed_user_id)).status_code == 404
    assert client.get(f"/api/v1/evaluations/{missing}/progress", headers=H(seed_user_id)).status_code == 404
    assert client.get(f"/api/v1/evaluations/ai/batches/{missing}", headers=H(seed_user_id)).status_code == 404


def test_aws_not_configured_returns_503(client: TestClient, seed_user_id: str, monkeypatch) -> None:
    from app.config import get_settings
    from app.pipeline.aws_jobs import Boto3AwsJobs

    settings = get_settings()
    for field in ("s3_bucket", "aws_app_access_key_id", "aws_app_secret_access_key"):
        monkeypatch.setattr(settings, field, "")
    app.dependency_overrides[get_aws_jobs] = lambda: Boto3AwsJobs()
    try:
        r = init_uploads(client, seed_user_id, [{"name": "a.pdf", "size": 100}])
    finally:
        app.dependency_overrides.pop(get_aws_jobs, None)
    assert r.status_code == 503
