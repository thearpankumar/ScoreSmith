# ruff: noqa: E501  (fixture tables and one-line fake-server routes read better unwrapped)
"""Tests for scripts/run_batch_evaluations.py against a local fake of the backend's AI-evaluation API.

Run: backend/.venv/Scripts/python.exe -m pytest scripts/tests -q   (stdlib + pytest only; no real network)
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import threading
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "run_batch_evaluations.py"
_spec = importlib.util.spec_from_file_location("run_batch_evaluations", SCRIPT)
rb = importlib.util.module_from_spec(_spec)
sys.modules["run_batch_evaluations"] = rb
_spec.loader.exec_module(rb)

SC_NAME = rb.DEFAULT_SCORECARD_NAME
SC_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())


# ------------------------------------------------------------------------------------------ fake backend


class FakeBackend:
    def __init__(self):
        self.lock = threading.Lock()
        self.part_size = 5 * 1024 * 1024
        self.scorecards = [{"id": SC_ID, "name": SC_NAME, "domain": "hackathon", "owner_id": USER_ID, "status": "published"}]
        self.users = [{"id": USER_ID, "name": "Evaluator", "email": "ev@example.com"}]
        self.uploads: dict[str, dict] = {}
        self.objects: dict[str, bytes] = {}
        self.evals: dict[str, dict] = {}
        self.jobs_calls: list[dict] = []
        self.init_calls = 0
        self.aborted: list[str] = []
        self.retry_calls: list[str] = []
        self.storage_off = False
        self.fail_attempts: dict[str, int] = {}  # email local part -> number of attempts that fail in the pipeline
        self.fail_complete: dict[str, int] = {}  # file-name substring -> remaining /complete rejections
        self.put_fail_once: set[str] = set()  # file-name substrings whose first PUT returns 500
        self.put_failures = 0
        self.server: ThreadingHTTPServer | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # --- request handling -------------------------------------------------------------------------
    def handle(self, method: str, path: str, body: bytes, headers) -> tuple[int, object | None, dict]:
        path = path.split("?")[0]
        data = json.loads(body) if body and headers.get("Content-Type", "").startswith("application/json") else None
        with self.lock:
            return self._route(method, path, data, body)

    def _route(self, method, path, data, raw):
        if method == "GET" and path == "/health":
            return 200, {"status": "ok"}, {}
        if method == "GET" and path == "/api/v1/scorecards":
            return 200, self.scorecards, {}
        if method == "GET" and (m := re.fullmatch(r"/api/v1/scorecards/([\w-]+)", path)):
            card = next((c for c in self.scorecards if c["id"] == m.group(1)), None)
            return (200, card, {}) if card else (404, {"detail": "Scorecard not found."}, {})
        if method == "GET" and path == "/api/v1/users":
            return 200, self.users, {}
        if method == "GET" and (m := re.fullmatch(r"/api/v1/users/([\w-]+)", path)):
            u = next((x for x in self.users if x["id"] == m.group(1)), None)
            return (200, u, {}) if u else (404, {"detail": "User not found."}, {})
        if method == "PUT" and (m := re.fullmatch(r"/s3/([\w-]+)/(\d+)", path)):
            up = self.uploads[m.group(1)]
            if any(s in up["name"] for s in self.put_fail_once) and m.group(2) == "1" and not up.get("failed_once"):
                up["failed_once"] = True
                self.put_failures += 1
                return 500, {"detail": "boom"}, {}
            up["parts"][int(m.group(2))] = raw
            return 200, None, {"ETag": '"' + hashlib.md5(raw).hexdigest() + '"'}  # noqa: S324
        base = "/api/v1/evaluations"
        if method == "POST" and path == f"{base}/ai/uploads":
            if self.storage_off:
                return 503, {"detail": "File storage is not configured: S3_BUCKET is not set."}, {}
            f = data["files"][0]
            ext = rb.file_extension(f["name"])
            if ext not in rb.SUBMISSION_EXTS:
                return 422, {"detail": [f"files[0] ({f['name']}): unsupported type"]}, {}
            self.init_calls += 1
            uid, key = str(uuid.uuid4()), f"uploads/{USER_ID}/{uuid.uuid4()}/{uuid.uuid4()}.{ext}"
            n_parts = max(1, -(-f["size"] // self.part_size))
            self.uploads[uid] = {"key": key, "name": f["name"], "size": f["size"], "parts": {}, "ext": ext}
            parts = [{"part_number": n, "url": f"{self.url}/s3/{uid}/{n}"} for n in range(1, n_parts + 1)]
            return 200, {"upload_group_id": str(uuid.uuid4()), "part_size": self.part_size,
                         "files": [{"client_index": 0, "upload_id": uid, "s3_key": key, "parts": parts}]}, {}
        if method == "POST" and path == f"{base}/ai/uploads/complete":
            out = []
            for f in data["files"]:
                up = self.uploads[f["upload_id"]]
                blob = b"".join(up["parts"][p["part_number"]] for p in sorted(f["parts"], key=lambda p: p["part_number"]))
                rejected = next((k for k, left in self.fail_complete.items() if k in up["name"] and left > 0), None)
                if rejected:
                    self.fail_complete[rejected] -= 1
                    out.append({"s3_key": f["s3_key"], "size": len(blob), "ok": False, "error": "The uploaded file is bad."})
                elif not rb.magic_ok(up["ext"], blob[:16]):
                    out.append({"s3_key": f["s3_key"], "size": len(blob), "ok": False,
                                "error": f"content does not look like a valid .{up['ext']} file"})
                else:
                    self.objects[f["s3_key"]] = blob
                    out.append({"s3_key": f["s3_key"], "size": len(blob), "ok": True, "error": None})
            return 200, {"files": out}, {}
        if method == "POST" and path == f"{base}/ai/uploads/abort":
            self.aborted += [f["upload_id"] for f in data["files"]]
            return 204, None, {}
        if method == "POST" and path == f"{base}/ai/jobs":
            if not any(c["id"] == data["scorecard_id"] for c in self.scorecards):
                return 404, {"detail": "Scorecard not found."}, {}
            errors = [{"item": i, "source": j, "error": "Invalid or foreign upload key."}
                      for i, it in enumerate(data["items"]) for j, s in enumerate(it["sources"]) if s["s3_key"] not in self.objects]
            if errors:
                return 422, {"detail": errors}, {}
            nonterminal = sum(1 for e in self.evals.values() if e["status"] not in ("completed", "failed"))
            self.jobs_calls.append({"n": len(data["items"]), "nonterminal_before": nonterminal, "items": data["items"]})
            batch = str(uuid.uuid4()) if len(data["items"]) > 1 else None
            created = []
            for it in data["items"]:
                eid = str(uuid.uuid4())
                self.evals[eid] = {"id": eid, "status": "queued", "polls": 0, "attempt": 1, "email": it["subject_email"],
                                   "sources": it["sources"], "batch_id": batch}
                created.append({"id": eid, "status": "queued", "batch_id": batch})
            return 202, {"batch_id": batch, "evaluations": created}, {}
        if m := re.fullmatch(rf"{base}/([\w-]+)/progress", path):
            ev = self.evals.get(m.group(1))
            if not ev:
                return 404, {"detail": "Evaluation not found."}, {}
            if ev["status"] not in ("completed", "failed"):
                ev["polls"] += 1
                local = (ev["email"] or "").split("@")[0]
                if ev["polls"] == 1:
                    ev["status"] = "queued"
                elif ev["polls"] == 2:
                    ev["status"] = "ingesting"
                elif ev["polls"] == 3:
                    ev["status"] = "scoring"
                elif ev["attempt"] <= self.fail_attempts.get(local, 0):
                    ev["status"] = "failed"
                else:
                    ev["status"] = "completed"
            failed = ev["status"] == "failed"
            return 200, {"evaluation_id": ev["id"], "status": ev["status"], "stage": ev["status"], "queue_position": None,
                         "error_code": "extract_failed" if failed else None,
                         "error_message": "could not extract" if failed else None, "progress": None,
                         "sources": [{"original_name": s["original_name"], "status": "done", "warnings": None}
                                     for s in ev["sources"]], "events": []}, {}
        if method == "GET" and (m := re.fullmatch(rf"{base}/([\w-]+)", path)):
            ev = self.evals.get(m.group(1))
            if not ev:
                return 404, {"detail": "Evaluation not found."}, {}
            return 200, {"id": ev["id"], "status": ev["status"], "final_weighted_score": 7.5, "rag_band": "acceptable"}, {}
        if method == "POST" and (m := re.fullmatch(rf"{base}/([\w-]+)/retry", path)):
            ev = self.evals.get(m.group(1))
            if not ev:
                return 404, {"detail": "Evaluation not found."}, {}
            if ev["status"] != "failed":
                return 409, {"detail": "Only failed evaluations can be retried."}, {}
            ev.update(status="queued", polls=0, attempt=ev["attempt"] + 1)
            self.retry_calls.append(ev["id"])
            return 202, {"id": ev["id"], "status": "queued"}, {}
        return 404, {"detail": f"no route {method} {path}"}, {}

    def start(self) -> None:
        backend = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                status, payload, extra = backend.handle(self.command, self.path, body, self.headers)
                raw = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PUT = _do

            def log_message(self, *a):  # silence
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeBackend()
    f.start()
    yield f
    f.stop()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(rb, "SLEEP", lambda s: None)


# ------------------------------------------------------------------------------------------ fixtures


def pdf(n=300, tag=b"") -> bytes:
    return b"%PDF-1.4\n" + tag + b"x" * n


def mp4(n=600, tag=b"") -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + tag + b"v" * n


def docx(tag=b"") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>" + tag.decode() + "</w:document>")
    return buf.getvalue()


def zipped() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "hello")
    return buf.getvalue()


def build_root(tmp_path: Path, students: dict[str, dict[str, bytes]]) -> Path:
    root = tmp_path / "GdriveDownload"
    root.mkdir()
    labels = {}
    for fid, files in students.items():
        d = root / fid
        d.mkdir()
        (d / ".manifest.json").write_text("{}")
        (d / "extracted_assets").mkdir()
        (d / "extracted_assets" / "0001_text.txt").write_text("must never be uploaded")
        for name, data in files.items():
            (d / name).write_bytes(data)
        labels[fid] = {"label": f"{fid}@srmist.edu.in - Student {fid.upper()}", "url": "u", "empty": None, "inaccessible": []}
    (root / ".summary_state.json").write_text(json.dumps(labels))
    return root


def standard_students() -> dict[str, dict[str, bytes]]:
    return {
        "s1": {"report.pdf": pdf(200, b"s1")},
        "s2": {"paper.docx": docx(b"s2"), "demo.mp4": mp4(1500, b"s2")},
        "s3": {"clip.mp4": mp4(900, b"s3")},
        "s4": {"a.pdf": pdf(400, b"s4"), "Demo video": mp4(700, b"s4"), "code.zip": zipped()},
        "s5": {"a.pdf": pdf(250, b"s5a"), "b.pdf": pdf(260, b"s5b"), "link.txt": b"https://github.com/x/y"},
        "s6": {"notes.md": b"# Notes\n" + b"hello world " * 20},
        "s7": {"Solution_Document": pdf(500, b"s7")},
    }


def base_args(fake: FakeBackend, root: Path, *extra: str) -> list[str]:
    return ["--gdrive-dir", str(root), "--api-base", fake.url, "--poll-interval", "0", "--yes",
            "--sheet", str(root / "none.xlsx"), "--part-workers", "2", *extra]


def state_of(root: Path) -> dict:
    return json.loads((root / ".batch_eval_state.json").read_text())["folders"]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ------------------------------------------------------------------------------------------ tests


def test_scan_shapes_and_magic_byte_detection(tmp_path):
    students = {**standard_students(), "empty1": {"only.zip": zipped()}, "txtonly": {"link.txt": b"https://x.y/z"}}
    root = build_root(tmp_path, students)
    got = {s.folder: s for s in rb.scan_all(root, tmp_path / "none.xlsx")[0]}
    assert [f.ext for f in got["s1"].files] == ["pdf"]
    assert sorted(f.ext for f in got["s2"].files) == ["docx", "mp4"]
    assert [f.name for f in got["s3"].files] == ["clip.mp4"]
    # extensionless download -> sent with a corrected, accepted name; the zip is skipped
    assert sorted(f.name for f in got["s4"].files) == ["Demo video.mp4", "a.pdf"]
    assert ("code.zip", "zip archive") in got["s4"].skipped
    # a link-only .txt is skipped when other files exist, but extracted_assets/ is never looked at
    assert [f.name for f in got["s5"].files] == ["a.pdf", "b.pdf"]
    assert any(n == "link.txt" for n, _ in got["s5"].skipped)
    assert [f.name for f in got["s7"].files] == ["Solution_Document.pdf"]
    assert got["s6"].files[0].ext == "md"
    assert got["empty1"].files == [] and got["empty1"].note == "no usable files"
    assert [f.name for f in got["txtonly"].files] == ["link.txt"]  # .txt is used when it is all there is
    assert got["s2"].email == "s2@srmist.edu.in" and got["s2"].name == "Student S2"


def test_sheet_identities_are_read_without_openpyxl(tmp_path):
    x = tmp_path / "subs.xlsx"
    with zipfile.ZipFile(x, "w") as z:
        z.writestr("xl/sharedStrings.xml",
                   '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   "<si><t>Timestamp</t></si><si><t>Email Address</t></si><si><t>Name</t></si>"
                   "<si><t>AB1@x.edu</t></si><si><t>Old Name</t></si><si><t>New Name</t></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml",
                   '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
                   '<row r="2"><c r="A2"><v>10</v></c><c r="B2" t="s"><v>3</v></c><c r="C2" t="s"><v>4</v></c></row>'
                   '<row r="3"><c r="A3"><v>20</v></c><c r="B3" t="s"><v>3</v></c><c r="C3" t="s"><v>5</v></c></row>'
                   "</sheetData></worksheet>")
    assert rb.read_sheet_identities(x) == {"ab1": ("ab1@x.edu", "New Name")}  # latest timestamp wins


def test_dry_run_makes_zero_requests(tmp_path, monkeypatch, capsys):
    root = build_root(tmp_path, standard_students())

    def boom(*a, **k):
        raise AssertionError("network used during --dry-run")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    rc = rb.main(["--gdrive-dir", str(root), "--sheet", str(root / "none.xlsx"), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Dry run: nothing was uploaded" in out and "7 student(s) to evaluate" in out
    assert "wave(s) of up to 5" in out
    assert not (root / ".batch_eval_state.json").exists()


def test_only_skip_limit_selection(tmp_path, capsys):
    root = build_root(tmp_path, standard_students())
    common = ["--gdrive-dir", str(root), "--sheet", str(root / "none.xlsx"), "--dry-run", "--no-canary"]
    rb.main([*common, "--only", "s1,s3"])
    assert "2 student(s) to evaluate" in capsys.readouterr().out
    rb.main([*common, "--skip", "s1,s2", "--limit", "3"])
    assert "3 student(s) to evaluate" in capsys.readouterr().out
    assert rb.main([*common, "--only", "nope"]) == 2


def test_check_preflight_uploads_nothing(tmp_path, fake, capsys):
    root = build_root(tmp_path, standard_students())
    assert rb.main(base_args(fake, root, "--check")) == 0
    out = capsys.readouterr().out
    assert "backend reachable" in out and "scorecard:" in out and "Preflight passed" in out
    assert fake.init_calls == 1 and len(fake.aborted) == 1 and not fake.objects and not fake.jobs_calls
    fake.storage_off = True
    assert rb.main(base_args(fake, root, "--check")) == 2
    assert "FAIL storage" in capsys.readouterr().out


def test_scorecard_name_resolution(fake):
    api = rb.Api(fake.url, USER_ID)
    ns = lambda **kw: type("NS", (), {"scorecard_id": None, "scorecard_name": SC_NAME, **kw})()  # noqa: E731
    assert rb.resolve_scorecard(api, ns())["id"] == SC_ID
    assert rb.resolve_scorecard(api, ns(scorecard_name=SC_NAME.upper()))["id"] == SC_ID  # case-insensitive
    assert rb.resolve_scorecard(api, ns(scorecard_name="vehicle intelligence"))["id"] == SC_ID  # unique contains
    assert rb.resolve_scorecard(api, ns(scorecard_id=SC_ID, scorecard_name="ignored"))["id"] == SC_ID
    fake.scorecards.append({"id": str(uuid.uuid4()), "name": SC_NAME + " (copy)", "domain": None, "owner_id": USER_ID})
    assert rb.resolve_scorecard(api, ns())["id"] == SC_ID  # exact still wins over contains
    with pytest.raises(rb.Fatal, match="ambiguous"):
        rb.resolve_scorecard(api, ns(scorecard_name="connected vehicle"))
    with pytest.raises(rb.Fatal, match="No scorecard named"):
        rb.resolve_scorecard(api, ns(scorecard_name="does not exist"))
    with pytest.raises(rb.Fatal, match="not found"):
        rb.resolve_scorecard(api, ns(scorecard_id=str(uuid.uuid4())))


def test_user_discovery(fake):
    api = rb.Api(fake.url, rb.NIL_UUID)
    ns = type("NS", (), {"user_id": None})()
    assert rb.resolve_user(api, ns, {"owner_id": USER_ID}) == USER_ID  # the scorecard's owner
    assert rb.resolve_user(api, ns, {"owner_id": str(uuid.uuid4())}) == USER_ID  # the only user
    fake.users.append({"id": str(uuid.uuid4()), "name": "Other", "email": "o@example.com"})
    with pytest.raises(rb.Fatal, match="--user-id"):
        rb.resolve_user(api, ns, {"owner_id": str(uuid.uuid4())})
    with pytest.raises(rb.Fatal, match="does not exist"):
        rb.resolve_user(api, type("NS", (), {"user_id": str(uuid.uuid4())})(), {})


def test_happy_path_canary_then_waves_of_five(tmp_path, fake):
    students = standard_students()
    root = build_root(tmp_path, students)
    rc = rb.main(base_args(fake, root))
    assert rc == 0
    # canary (1), then the other six as a wave of 5 + a wave of 1
    assert [c["n"] for c in fake.jobs_calls] == [1, 5, 1]
    assert all(c["nonterminal_before"] == 0 for c in fake.jobs_calls)  # each wave starts after the previous finished
    assert len(fake.evals) == 7 and all(e["status"] == "completed" for e in fake.evals.values())
    st = state_of(root)
    assert {f: r["status"] for f, r in st.items()} == dict.fromkeys(students, "completed")
    # exactly the usable files were uploaded, byte for byte (zip / .txt / extracted_assets never)
    expected = {sha(d) for files in students.values() for n, d in files.items() if not n.endswith((".zip", ".txt"))}
    assert {sha(b) for b in fake.objects.values()} == expected
    # identity travelled with the job
    emails = {i["subject_email"] for c in fake.jobs_calls for i in c["items"]}
    assert emails == {f"{f}@srmist.edu.in" for f in students}
    rep = json.loads((root / ".batch_eval_report.json").read_text())
    assert len(rep["students"]) == 7 and (root / ".batch_eval_report.csv").exists()
    assert fake.aborted == []


def test_multipart_slicing_reassembles_exactly(tmp_path, fake):
    fake.part_size = 1000
    blob = pdf(3490, b"multi")  # 3500 bytes -> 4 parts
    root = build_root(tmp_path, {"m1": {"big.pdf": blob}})
    assert rb.main(base_args(fake, root)) == 0
    assert list(fake.objects.values()) == [blob]
    assert len(next(iter(fake.uploads.values()))["parts"]) == 4


def test_part_upload_is_retried_after_a_server_error(tmp_path, fake):
    fake.put_fail_once = {"a.pdf"}
    root = build_root(tmp_path, {"r1": {"a.pdf": pdf(100)}})
    assert rb.main(base_args(fake, root)) == 0
    assert fake.put_failures == 1 and len(fake.objects) == 1


def test_canary_failure_stops_the_run(tmp_path, fake, capsys):
    root = build_root(tmp_path, standard_students())
    students, _ = rb.scan_all(root, tmp_path / "none.xlsx")
    canary = rb.pick_canary(rb.build_parser().parse_args([]), [s.folder for s in students], {s.folder: s for s in students})
    fake.fail_attempts = {canary: 99}
    rc = rb.main(base_args(fake, root))
    out = capsys.readouterr().out
    assert rc == 2 and "Canary did NOT complete" in out and "extract_failed" in out
    assert len(fake.jobs_calls) == 1
    assert state_of(root)[canary]["status"] == "failed"


def test_canary_gate_can_be_overridden(tmp_path, fake):
    root = build_root(tmp_path, standard_students())
    students, _ = rb.scan_all(root, tmp_path / "none.xlsx")
    canary = rb.pick_canary(rb.build_parser().parse_args([]), [s.folder for s in students], {s.folder: s for s in students})
    fake.fail_attempts = {canary: 99}
    rc = rb.main(base_args(fake, root, "--no-canary-gate"))
    st = state_of(root)
    assert rc == 1  # the canary stays failed after the retry rounds, everyone else completed
    assert st[canary]["status"] == "failed" and sum(r["status"] == "completed" for r in st.values()) == 6


def test_failed_evaluation_is_queued_and_retried_later(tmp_path, fake):
    root = build_root(tmp_path, standard_students())
    fake.fail_attempts = {"s3": 1}  # fails the first time only
    assert rb.main(base_args(fake, root, "--no-canary")) == 0
    assert len(fake.retry_calls) == 1  # POST /retry, not a second upload
    assert state_of(root)["s3"]["status"] == "completed" and state_of(root)["s3"]["attempts"] == 2
    assert len(fake.uploads) == 10  # the 10 usable files were uploaded once each; no re-upload for the retry


def test_persistent_failure_ends_in_the_report_after_the_retry_rounds(tmp_path, fake):
    root = build_root(tmp_path, standard_students())
    fake.fail_attempts = {"s3": 99}
    rc = rb.main(base_args(fake, root, "--no-canary", "--max-retry-rounds", "2"))
    assert rc == 1 and len(fake.retry_calls) == 2
    rep = {r["folder"]: r for r in json.loads((root / ".batch_eval_report.json").read_text())["students"]}
    assert rep["s3"]["status"] == "failed" and rep["s3"]["error_code"] == "extract_failed"
    assert rep["s1"]["status"] == "completed"


def test_upload_rejection_aborts_then_reuploads_in_the_retry_round(tmp_path, fake):
    root = build_root(tmp_path, {"u1": {"a.pdf": pdf(100), "z.mp4": mp4(200)}, "u2": {"b.pdf": pdf(120)}})
    fake.fail_complete = {"z.mp4": 1}
    rc = rb.main(base_args(fake, root, "--no-canary"))
    assert rc == 0
    assert fake.aborted, "the rejected multipart upload must be aborted"
    st = state_of(root)
    assert st["u1"]["status"] == "completed" and st["u1"]["attempts"] == 1
    assert any(e["where"] == "upload" for e in st["u1"]["errors"])
    assert [c["n"] for c in fake.jobs_calls] == [1, 1]  # u2 first (wave), then u1 after its re-upload


def test_resume_reattaches_and_never_resubmits(tmp_path, fake, capsys):
    root = build_root(tmp_path, {"d1": {"a.pdf": pdf(100)}, "d2": {"b.pdf": pdf(120)}, "d3": {"c.pdf": pdf(140)}})
    # state left by an interrupted run: d1 completed, d2 still evaluating on the server, d3 untouched
    eid = str(uuid.uuid4())
    fake.evals[eid] = {"id": eid, "status": "queued", "polls": 0, "attempt": 1, "email": "d2@srmist.edu.in",
                       "sources": [], "batch_id": None}
    stt = rb.State(root / ".batch_eval_state.json", SC_ID)
    stt.update("d1", status="completed", evaluation_id=str(uuid.uuid4()), attempts=1, score=8.0)
    stt.update("d2", status="evaluating", evaluation_id=eid, attempts=1)
    assert rb.main(base_args(fake, root)) == 2  # refuses without --resume
    assert "--resume" in capsys.readouterr().out and fake.init_calls == 0
    assert rb.main(base_args(fake, root, "--resume")) == 0
    st = state_of(root)
    assert {f: r["status"] for f, r in st.items()} == {"d1": "completed", "d2": "completed", "d3": "completed"}
    assert fake.init_calls == 1  # only d3 was uploaded
    assert [c["n"] for c in fake.jobs_calls] == [1]  # only d3 got a new evaluation


def test_interrupt_saves_state_and_exits_130(tmp_path, fake, monkeypatch):
    root = build_root(tmp_path, {"i1": {"a.pdf": pdf(100)}})

    def ctrl_c(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(rb, "SLEEP", ctrl_c)
    assert rb.main(base_args(fake, root, "--no-canary")) == 130
    assert state_of(root)["i1"]["status"] == "evaluating"  # can be re-attached with --resume


def test_storage_not_configured_is_fatal_not_per_student(tmp_path, fake, capsys):
    fake.storage_off = True
    root = build_root(tmp_path, standard_students())
    assert rb.main(base_args(fake, root)) == 2
    out = capsys.readouterr().out
    assert "not configured" in out and "bootstrap" in out
    assert fake.jobs_calls == []


def test_requires_confirmation_without_yes(tmp_path, fake, monkeypatch, capsys):
    root = build_root(tmp_path, standard_students())
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    args = [a for a in base_args(fake, root) if a != "--yes"]
    assert rb.main(args) == 2
    assert "--yes" in capsys.readouterr().out and fake.init_calls == 0
