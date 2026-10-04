import json
import zipfile
from pathlib import Path

import pytest

from fakes import FakeResponse, FakeSession, MemoryStore, public_resolver
from worker import corpus, docs
from worker.drive import DriveClient
from worker.errors import PipelineError
from worker.ingest import run_ingest
from worker.progress import Progress, merge_progress

EID = "e1"
PDF = b"%PDF-1.4\n" + b"x" * 100
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\0" * 50


def docx_bytes(tmp_path, text="Hello docx"):
    from docx import Document

    p = tmp_path / "t.docx"
    d = Document()
    d.add_paragraph(text)
    d.save(p)
    return p.read_bytes()


def ev(sources, **limits):
    return {"evaluation_id": EID, "bucket": "b", "limits": {"max_file_bytes": 10_000, "max_files": 20, **limits}, "sources": sources}


def drive_client(routes=None, listing=None):
    return DriveClient(session=FakeSession(routes or {}), list_folder=lambda fid: listing or [], resolver=public_resolver,
                       sleep=lambda s: None, log=lambda m: None)


# ----------------------------------------------------------------------------- ingest: uploads
def test_ingest_uploads_ok_and_manifest(tmp_path):
    s = MemoryStore()
    s.put_bytes("uploads/u/g/1.pdf", PDF)
    s.put_bytes("uploads/u/g/2.mp4", MP4)
    s.put_bytes("uploads/u/g/3.docx", docx_bytes(tmp_path))
    out = run_ingest(ev([
        {"source_id": "a", "kind": "upload", "s3_key": "uploads/u/g/1.pdf", "original_name": "Plan.pdf"},
        {"source_id": "b", "kind": "upload", "s3_key": "uploads/u/g/2.mp4", "original_name": "demo.mp4"},
        {"source_id": "c", "kind": "upload", "s3_key": "uploads/u/g/3.docx", "original_name": "r.docx"}], max_file_bytes=10**7), s)
    assert [f["kind"] for f in out["files"]] == ["pdf", "video", "docx"]
    assert s.exists("raw/e1/a.pdf") and s.exists("raw/e1/b.mp4") and s.exists("raw/e1/c.docx")
    m = s.get_json("derived/e1/manifest.json")
    assert m["drive"] == [] and all(f["status"] == "ok" for f in m["files"])
    prog = s.get_json("derived/e1/progress.json")
    assert prog["stage"] == "extract" and prog["counters"]["files_total"] == 3


def test_ingest_rejects_bad_magic_oversize_and_foreign_key(tmp_path):
    s = MemoryStore()
    s.put_bytes("uploads/u/g/fake.pdf", b"<html>not a pdf</html>")
    s.put_bytes("uploads/u/g/big.pdf", PDF + b"x" * 20_000)
    s.put_bytes("uploads/u/g/ok.pdf", PDF)
    s.put_bytes("derived/e2/secret.pdf", PDF)
    out = run_ingest(ev([
        {"source_id": "a", "kind": "upload", "s3_key": "uploads/u/g/fake.pdf", "original_name": "fake.pdf"},
        {"source_id": "b", "kind": "upload", "s3_key": "uploads/u/g/big.pdf", "original_name": "big.pdf"},
        {"source_id": "c", "kind": "upload", "s3_key": "derived/e2/secret.pdf", "original_name": "s.pdf"},
        {"source_id": "d", "kind": "upload", "s3_key": "uploads/u/g/ok.pdf", "original_name": "ok.pdf"}]), s)
    assert [f["source_id"] for f in out["files"]] == ["d"]
    st = {f["source_id"]: f["status"] for f in s.get_json("derived/e1/manifest.json")["files"]}
    assert st == {"a": "failed", "b": "skipped", "c": "failed", "d": "ok"}


def test_ingest_max_files_enforced():
    s = MemoryStore()
    srcs = []
    for i in range(4):
        s.put_bytes(f"uploads/u/g/{i}.pdf", PDF)
        srcs.append({"source_id": f"s{i}", "kind": "upload", "s3_key": f"uploads/u/g/{i}.pdf", "original_name": f"{i}.pdf"})
    out = run_ingest(ev(srcs, max_files=2), s)
    assert len(out["files"]) == 2
    assert any("file limit" in w for f in s.get_json("derived/e1/manifest.json")["files"] for w in f["warnings"])


def test_ingest_all_too_large_raises_file_too_large():
    s = MemoryStore()
    s.put_bytes("uploads/u/g/big.pdf", PDF + b"x" * 20_000)
    with pytest.raises(PipelineError) as e:
        run_ingest(ev([{"source_id": "b", "kind": "upload", "s3_key": "uploads/u/g/big.pdf", "original_name": "big.pdf"}]), s)
    assert e.value.code == "file_too_large" and str(e.value).startswith("file_too_large:")


# ----------------------------------------------------------------------------- ingest: drive
DL = "https://drive.usercontent.google.com/download"


def test_ingest_drive_folder_mixed(tmp_path):
    s = MemoryStore()
    routes = {DL + "?id=F1": FakeResponse(200, {"content-type": "application/pdf"}, PDF),
              DL + "?id=F2": FakeResponse(200, {"content-type": "application/pdf"}, PDF)}
    listing = [("F1", "report.pdf"), ("F2", "notes.pdf"), ("F3", "photo.png")]
    d = drive_client(routes, listing)
    out = run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "https://drive.google.com/drive/folders/FOLD"}]), s, drive=d)
    assert [f["source_id"] for f in out["files"]] == ["s1-01", "s1-02"]
    m = s.get_json("derived/e1/manifest.json")
    assert m["drive"][0]["state"] == "ok" and m["drive"][0]["files_found"] == 3
    assert m["files"][2]["status"] == "skipped" and m["files"][0]["parent_source_id"] == "s1"
    assert s.exists("raw/e1/s1-01.pdf")


def test_ingest_drive_identifies_extensionless_files_by_content(tmp_path):
    """A Drive file named like 'report (final)' has no extension: it must be sniffed, not skipped. This was a
    real submission whose main report (a DOCX and a PDF) was dropped as 'unsupported'."""
    s = MemoryStore()
    docx = docx_bytes(tmp_path)
    routes = {DL + "?id=F1": FakeResponse(200, {"content-type": "application/octet-stream"}, docx),
              DL + "?id=F2": FakeResponse(200, {"content-type": "application/octet-stream"}, PDF)}
    listing = [("F1", "report (contains all required links and information)"),
               ("F2", "report (contains all required links and information) (1ez0Fz)"), ("F3", "code.zip")]
    d = drive_client(routes, listing)
    out = run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "https://drive.google.com/drive/folders/FOLD"}],
                        max_file_bytes=10**9), s, drive=d)
    kinds = {f["source_id"]: (f["kind"], f["status"]) for f in s.get_json("derived/e1/manifest.json")["files"]}
    assert kinds["s1-01"] == ("docx", "ok") and kinds["s1-02"] == ("pdf", "ok")
    assert kinds["s1-03"][1] == "skipped"  # a .zip is still unsupported, without being downloaded
    assert len(out["files"]) == 2


@pytest.mark.parametrize("listing_exc,code", [
    (RuntimeError("Cannot retrieve the public link of the file. 'Anyone with the link'"), "drive_inaccessible"),
    (RuntimeError("Too many users have viewed or downloaded this file recently"), "drive_quota"),
])
def test_ingest_drive_failure_codes(listing_exc, code):
    def boom(fid):
        raise listing_exc

    d = DriveClient(session=FakeSession({}), list_folder=boom, resolver=public_resolver, sleep=lambda s: None, log=lambda m: None)
    with pytest.raises(PipelineError) as e:
        run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "https://drive.google.com/drive/folders/FOLD"}]), MemoryStore(), drive=d)
    assert e.value.code == code


def test_ingest_drive_empty_and_invalid():
    with pytest.raises(PipelineError) as e:
        run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "https://drive.google.com/drive/folders/FOLD"}]), MemoryStore(), drive=drive_client(listing=[]))
    assert e.value.code == "drive_empty"
    with pytest.raises(PipelineError) as e:
        run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "http://169.254.169.254/latest"}]), MemoryStore(), drive=drive_client())
    assert e.value.code == "drive_invalid"


def test_ingest_drive_rejects_html_masquerading_as_pdf():
    d = drive_client({DL + "?id=F1": FakeResponse(200, {"content-type": "application/pdf"}, b"<html>captcha</html>")}, [("F1", "x.pdf")])
    s = MemoryStore()
    with pytest.raises(PipelineError) as e:
        run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "https://drive.google.com/drive/folders/FOLD"}]), s, drive=d)
    assert e.value.code == "unsupported_type"
    assert not any(k.startswith("raw/") for k in s.objects)


def test_ingest_drive_video_probe():
    d = drive_client({DL + "?id=V1": FakeResponse(200, {"content-type": "video/mp4"}, MP4)}, [("V1", "demo.mp4")])
    s = MemoryStore()
    out = run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "https://drive.google.com/drive/folders/FOLD"}]), s, drive=d,
                     probe_fn=lambda p: (30.0, True, True))
    assert out["files"][0]["kind"] == "video"
    with pytest.raises(PipelineError):
        run_ingest(ev([{"source_id": "s1", "kind": "drive", "drive_url": "https://drive.google.com/drive/folders/FOLD"}]), MemoryStore(),
                   drive=d, probe_fn=lambda p: (0.0, False, False))


# ----------------------------------------------------------------------------- docs extraction (real pymupdf / python-docx)
def make_pdf(path: Path):
    import pymupdf

    doc = pymupdf.open()
    for n in (1, 2):
        page = doc.new_page()
        page.insert_text((72, 100), f"Hello page {n}. This is the project overview.")
    doc.save(path)


def test_extract_pdf_and_docx(tmp_path):
    pdf = tmp_path / "a.pdf"
    make_pdf(pdf)
    st = docs.extract_file(str(pdf), "pdf", tmp_path / "o1")
    md = (tmp_path / "o1" / "content.md").read_text()
    assert "## Page 1" in md and "Hello page 2" in md and st.images == 0
    dx = tmp_path / "a.docx"
    dx.write_bytes(docx_bytes(tmp_path, "Docx body text"))
    docs.extract_file(str(dx), "docx", tmp_path / "o2")
    assert "Docx body text" in (tmp_path / "o2" / "content.md").read_text()
    with pytest.raises(PipelineError):
        docs.extract_file(str(dx), "pdf", tmp_path / "o3")  # zip is not a PDF
    fake = tmp_path / "f.docx"
    fake.write_bytes(b"nope")
    with pytest.raises(PipelineError):
        docs.extract_file(str(fake), "docx", tmp_path / "o4")


def test_output_cap_stops_zip_bomb_style_inputs(tmp_path):
    em = docs.Emitter(tmp_path / "o", docs.ExtractConfig(max_output_bytes=100))
    with pytest.raises(PipelineError, match="too much image data"):
        em._save(b"x" * 500, "image", "jpg")


def test_run_extract_doc_uploads_and_progress(tmp_path):
    s = MemoryStore()
    pdf = tmp_path / "a.pdf"
    make_pdf(pdf)
    s.put_bytes("raw/e1/d1.pdf", pdf.read_bytes())
    out = docs.run_extract_doc({"evaluation_id": EID, "bucket": "b", "file": {"source_id": "d1", "kind": "pdf", "raw_key": "raw/e1/d1.pdf", "original_name": "a.pdf"}}, s, tmp_root=str(tmp_path))
    assert out["images"] == [] and "Hello page 1" in s.get_text("derived/e1/docs/d1/content.md")
    assert s.get_json("derived/e1/progress.json")["files"][0]["state"] == "done"
    s.put_bytes("raw/e1/d2.pdf", b"garbage")
    out = docs.run_extract_doc({"evaluation_id": EID, "bucket": "b", "file": {"source_id": "d2", "kind": "pdf", "raw_key": "raw/e1/d2.pdf", "original_name": "bad.pdf"}}, s, tmp_root=str(tmp_path))
    assert "error" in out and s.get_json("derived/e1/docs/d2/extract.json")["code"] == "unsupported_type"


# ----------------------------------------------------------------------------- progress
def test_merge_progress_counters_and_stage():
    files = [{"source_id": "a", "name": "a.pdf", "state": "running", "images_total": 3},
             {"source_id": "v", "name": "v.mp4", "state": "running", "chunks_total": 2},
             {"source_id": "z", "name": "z.pdf", "state": "failed", "detail": "bad"}]
    keys = ["derived/e/progress/img/a/0001.done", "derived/e/progress/img/a/0002.failed", "derived/e/progress/chunk/v/1.done"]
    doc = merge_progress({"stage": "extract", "message": "m"}, files, keys, now="t")
    c = doc["counters"]
    assert (c["images_total"], c["images_done"], c["chunks_total"], c["chunks_done"]) == (3, 2, 2, 1)
    assert doc["stage"] == "analyze" and c["files_done"] == 1 and c["files_total"] == 3
    keys += ["derived/e/progress/img/a/0003.done", "derived/e/progress/chunk/v/2.done"]
    doc = merge_progress({"stage": "extract"}, files, keys)
    assert doc["stage"] == "extract" and {f["source_id"]: f["state"] for f in doc["files"]} == {"a": "done", "v": "done", "z": "failed"}


def test_progress_refresh_writes_merged_doc():
    s = MemoryStore()
    p = Progress(s, EID)
    p.set_stage("extract", "go")
    p.file("a", "a.pdf", "running", images_total=1)
    p.mark("img", "a", "0001", True)
    doc = s.get_json("derived/e1/progress.json")
    assert set(doc) == {"stage", "updated_at", "message", "files", "counters"}


# ----------------------------------------------------------------------------- assemble / fail
def seed_assemble(s):
    s.put_json("derived/e1/manifest.json", {"drive": [{"source_id": "x", "url": "u", "state": "ok", "files_found": 1, "warnings": ["Ignored 2 file(s) inside subfolders"]}],
                                            "files": [
        {"source_id": "d1", "original_name": "a.pdf", "kind": "pdf", "raw_key": "raw/e1/d1.pdf", "size": 1, "status": "ok", "warnings": []},
        {"source_id": "v1", "original_name": "demo.mp4", "kind": "video", "raw_key": "raw/e1/v1.mp4", "size": 1, "status": "ok", "warnings": []},
        {"source_id": "x1", "original_name": "p.png", "kind": "other", "raw_key": "", "size": 1, "status": "skipped", "warnings": ["Unsupported file type skipped: p.png"]}]})
    s.put_text("derived/e1/docs/d1/content.md",
               "## Page 1\n\nIntro text.\n\n![0002_p01_image.jpg](0002_p01_image.jpg)\n\nAfter image.\n\n## Page 2\n\nSecond page.\n\n![0004_p02_figure.jpg](0004_p02_figure.jpg)\n")
    s.put_text("derived/e1/docs/d1/images/0002_p01_image.analysis.md", "<!-- source: x | model: y -->\n\n## Summary\nA diagram.\n")
    s.put_json("derived/e1/video/v1/plan.json", {"has_audio": True, "duration_sec": 700, "chunks": [
        {"index": 1, "key": "k1", "start": 0, "end": 300}, {"index": 2, "key": "k2", "start": 300, "end": 700}]})
    s.put_text("derived/e1/video/v1/chunks/c1.txt", "First chunk speech.")


def test_assemble_builds_corpus_in_reading_order(monkeypatch):
    monkeypatch.setattr(corpus, "MIN_CORPUS_WORDS", 1)  # this fixture is deliberately tiny
    s = MemoryStore()
    seed_assemble(s)
    out = corpus.run_assemble({"evaluation_id": EID, "bucket": "b"}, s)
    c = s.get_json("derived/e1/corpus.json")
    labels = [x["label"] for x in c["sections"]]
    assert labels == ["DOC a.pdf p1", "IMAGE a.pdf fig 2", "DOC a.pdf p1", "DOC a.pdf p2", "VIDEO demo.mp4 00:00-05:00"]
    assert [x["id"] for x in c["sections"]] == ["s001", "s002", "s003", "s004", "s005"]
    assert c["stats"] == {"docs": 1, "videos": 1, "images": 1, "words": c["stats"]["words"]} and c["stats"]["words"] > 8
    assert any("not analysed" in w for w in c["warnings"]) and any("1 of 2 audio chunk" in w for w in c["warnings"])
    assert any("Unsupported" in w for w in c["warnings"]) and any("subfolders" in w for w in c["warnings"])
    assert set(c) == {"evaluation_id", "built_at", "stats", "warnings", "sections"}
    assert "## [DOC a.pdf p1]" in s.get_text("derived/e1/corpus.md")
    assert s.get_text("derived/e1/video/v1/transcript.txt").strip() == "First chunk speech."
    assert s.get_json("derived/e1/video/v1/transcript.json")["missing_chunks"] == [2]
    p = s.get_json("derived/e1/progress.json")
    assert p["stage"] == "done" and out["corpus_key"] == "derived/e1/corpus.json"


def test_assemble_no_content_raises_and_fail_writes_status():
    s = MemoryStore()
    s.put_json("derived/e1/manifest.json", {"drive": [], "files": [
        {"source_id": "d1", "original_name": "a.pdf", "kind": "pdf", "raw_key": "r", "size": 1, "status": "ok", "warnings": []}]})
    s.put_text("derived/e1/docs/d1/content.md", "## Page 1\n\n   \n")
    with pytest.raises(PipelineError) as e:
        corpus.run_assemble({"evaluation_id": EID, "bucket": "b"}, s)
    assert e.value.code == "no_content" and not s.exists("derived/e1/corpus.json")
    err = {"Error": "PipelineError", "Cause": json.dumps({"errorMessage": str(e.value), "errorType": "PipelineError"})}
    out = corpus.run_fail({"mode": "fail", "evaluation_id": EID, "bucket": "b", "error": err}, s)
    assert out["error_code"] == "no_content"
    assert s.get_json("derived/e1/status.json")["error_code"] == "no_content"
    assert s.get_json("derived/e1/progress.json")["stage"] == "failed"


def test_fail_handler_never_raises_without_manifest():
    s = MemoryStore()
    out = corpus.run_fail({"evaluation_id": EID, "bucket": "b", "error": {"Error": "States.Timeout", "Cause": ""}}, s)
    assert out["error_code"] == "timeout" and s.get_json("derived/e1/progress.json")["stage"] == "failed"


def test_long_docx_text_split_into_sections():
    s = MemoryStore()
    s.put_json("derived/e1/manifest.json", {"drive": [], "files": [
        {"source_id": "d1", "original_name": "big.docx", "kind": "docx", "raw_key": "r", "size": 1, "status": "ok", "warnings": []}]})
    s.put_text("derived/e1/docs/d1/content.md", "\n\n".join(f"Paragraph {i} " + "word " * 300 for i in range(10)))
    corpus.run_assemble({"evaluation_id": EID, "bucket": "b"}, s)
    c = s.get_json("derived/e1/corpus.json")
    assert len(c["sections"]) > 1 and c["sections"][1]["label"] == "DOC big.docx part 2"
    assert all(len(x["text"]) <= corpus.MAX_SECTION_CHARS for x in c["sections"])


def test_handlers_importable():
    from worker import handlers

    for n in ("ingest", "extract_doc", "plan_audio", "transcribe_chunk", "analyze_image", "assemble"):
        assert callable(getattr(handlers, n))


# ----------------------------------------------------------------------------- markdown / text documents


def test_extract_text_file_is_paged_like_a_document(tmp_path):
    body = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(12))
    src = tmp_path / "SOLUTION.md"
    src.write_text("# Solution\n\n" + body, encoding="utf-8")
    out = tmp_path / "out"
    docs.extract_file(str(src), "text", out)
    md = (out / "content.md").read_text(encoding="utf-8")
    assert "## Page 1" in md and "## Page 2" in md and "Paragraph 11." in md


def test_extract_text_file_rejects_binary(tmp_path):
    src = tmp_path / "x.txt"
    src.write_bytes(bytes([0x61, 0x62, 0x63, 0x00, 0x01, 0x02]) + b"binary")
    with pytest.raises(PipelineError) as e:
        docs.extract_file(str(src), "text", tmp_path / "out")
    assert e.value.code == "unsupported_type"


def test_ingest_accepts_markdown_and_text_but_not_binary_renamed_txt():
    s = MemoryStore()
    s.put_bytes("uploads/u/g/a.md", b"# Solution\n\n" + b"real text " * 50)
    s.put_bytes("uploads/u/g/b.txt", bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0x00, 0x00]) + b" not text")
    out = run_ingest(ev([
        {"source_id": "a", "kind": "upload", "s3_key": "uploads/u/g/a.md", "original_name": "SOLUTION.md"},
        {"source_id": "b", "kind": "upload", "s3_key": "uploads/u/g/b.txt", "original_name": "notes.txt"},
    ]), s)
    m = {f["source_id"]: (f["kind"], f["status"]) for f in s.get_json("derived/e1/manifest.json")["files"]}
    assert m["a"] == ("text", "ok") and m["b"][1] == "failed"
    assert [f["source_id"] for f in out["files"]] == ["a"]


def test_assemble_rejects_a_corpus_with_almost_no_words():
    """A 49-byte 'github-link.txt' is not a submission: fail clearly instead of scoring nothing."""
    s = MemoryStore()
    s.put_json("derived/e1/manifest.json", {"drive": [], "files": [
        {"source_id": "t1", "original_name": "github-link.txt", "kind": "text", "raw_key": "r", "size": 49,
         "status": "ok", "warnings": []}]})
    s.put_text("derived/e1/docs/t1/content.md", "## Page 1\n\nhttps://github.com/someone/some-repo\n")
    with pytest.raises(PipelineError) as e:
        corpus.run_assemble({"evaluation_id": EID, "bucket": "b"}, s)
    assert e.value.code == "no_content" and "not enough" in e.value.message


def test_silent_video_is_skipped_not_failed_and_the_document_still_scores():
    """A screen recording without an audio track has nothing to transcribe. The file is 'skipped' (not
    'failed'), a warning is kept, and the evaluation proceeds from the document."""
    s = MemoryStore()
    s.put_json("derived/e1/manifest.json", {"drive": [], "files": [
        {"source_id": "d1", "original_name": "solution.pdf", "kind": "pdf", "raw_key": "r1", "size": 1,
         "status": "ok", "warnings": []},
        {"source_id": "v1", "original_name": "demo.mp4", "kind": "video", "raw_key": "r2", "size": 1,
         "status": "ok", "warnings": []}]})
    s.put_text("derived/e1/docs/d1/content.md", "## Page 1\n\n" + "A real solution document sentence. " * 12)
    s.put_json("derived/e1/video/v1/plan.json", {"has_audio": False, "chunks": []})
    prog = Progress(s, EID)  # earlier steps register every file, assemble sets the final states
    prog.file("d1", "solution.pdf", "pending")
    prog.file("v1", "demo.mp4", "pending")
    corpus.run_assemble({"evaluation_id": EID, "bucket": "b"}, s)
    states = {f["source_id"]: f["state"] for f in s.get_json("derived/e1/progress.json")["files"]}
    assert states == {"d1": "done", "v1": "skipped"}
    c = s.get_json("derived/e1/corpus.json")
    assert c["stats"]["videos"] == 0 and c["stats"]["docs"] == 1
    assert any("no audio track" in w for w in c["warnings"])
