import zipfile

import pytest

from fakes import FakeResponse, FakeSession, public_resolver
from worker import drive as drive_mod
from worker import security
from worker.drive import DriveClient, DriveError, classify, parse_link
from worker.errors import PipelineError, code_from_error


# ----------------------------------------------------------------------------- security
@pytest.mark.parametrize("ip,ok", [
    ("142.250.80.46", True), ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.0.5", False),
    ("169.254.169.254", False), ("172.16.0.1", False), ("::1", False), ("fe80::1", False),
    ("::ffff:127.0.0.1", False), ("0.0.0.0", False), ("2607:f8b0:4004:c07::71", True),
])
def test_is_public_ip(ip, ok):
    assert security.is_public_ip(ip) is ok


@pytest.mark.parametrize("url", [
    "http://drive.google.com/file/d/abc/view",          # not https
    "https://evil.com/file/d/abc/view",                 # host
    "https://drive.google.com.evil.com/file/d/abc",     # suffix trick
    "https://user:pw@drive.google.com/file/d/abc",      # credentials
    "https://drive.google.com:8443/file/d/abc",         # port
    "https://169.254.169.254/latest/meta-data",
    "ftp://drive.google.com/x",
    "",
])
def test_check_url_rejects(url):
    with pytest.raises(PipelineError) as e:
        security.check_url(url, resolver=public_resolver)
    assert e.value.code == "drive_invalid"


def test_check_url_blocks_private_resolution():
    with pytest.raises(PipelineError, match="non-public"):
        security.check_url("https://drive.google.com/file/d/x", resolver=lambda h: ["10.0.0.5"])
    with pytest.raises(PipelineError, match="non-public"):  # any private answer poisons the set
        security.check_url("https://drive.google.com/file/d/x", resolver=lambda h: ["142.250.80.46", "127.0.0.1"])


def test_check_url_accepts_allowlist_and_redirect_suffix():
    for h in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        assert security.check_url(f"https://{h}/x", resolver=public_resolver) == h
    with pytest.raises(PipelineError):
        security.check_url("https://doc-1.googleusercontent.com/x", resolver=public_resolver)  # not for first hop
    assert security.check_url("https://doc-1.googleusercontent.com/x", redirect=True, resolver=public_resolver)


def test_magic_bytes(tmp_path):
    assert security.check_pdf_head(b"%PDF-1.7 ...") and not security.check_pdf_head(b"<html>")
    assert security.check_video_head(b"\x00\x00\x00\x18ftypmp42") and security.check_video_head(b"\x1a\x45\xdf\xa3xx")
    assert not security.check_video_head(b"hello world!!")
    good = tmp_path / "a.docx"
    with zipfile.ZipFile(good, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    security.check_docx(str(good))
    bad = tmp_path / "b.docx"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("other.txt", "x")
    with pytest.raises(PipelineError):
        security.check_docx(str(bad))
    notzip = tmp_path / "c.docx"
    notzip.write_bytes(b"not a zip")
    with pytest.raises(PipelineError):
        security.check_docx(str(notzip))


def test_docx_zip_bomb(tmp_path):
    p = tmp_path / "bomb.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", "<w/>")
        z.writestr("word/media/big.bin", b"\0" * 60_000_000)  # compresses ~1000:1
    with pytest.raises(PipelineError, match="compression ratio"):
        security.check_docx(str(p))


def test_kind_detection():
    assert security.kind_of("a.PDF") == "pdf" and security.kind_of("x.mov") == "video" and security.kind_of("x.zip") == "other"


# ----------------------------------------------------------------------------- drive parsing
def test_parse_link():
    assert parse_link("https://drive.google.com/drive/folders/1AbC_d-e?usp=sharing") == ("folder", "1AbC_d-e")
    assert parse_link("https://drive.google.com/file/d/FILEID/view") == ("file", "FILEID")
    assert parse_link("https://docs.google.com/document/d/DOCID/edit") == ("document", "DOCID")
    assert parse_link("https://docs.google.com/spreadsheets/d/S1/edit") == ("spreadsheets", "S1")
    assert parse_link("https://docs.google.com/presentation/d/P1/edit") == ("presentation", "P1")
    assert parse_link("https://drive.google.com/open?id=XYZ") == ("open", "XYZ")
    assert parse_link("https://drive.google.com/") is None


def test_classify_rejects_bad_host():
    with pytest.raises(PipelineError):
        classify("https://example.com/file/d/abc", resolver=public_resolver)
    with pytest.raises(PipelineError, match="Unrecognised"):
        classify("https://drive.google.com/nothing", resolver=public_resolver)


# ----------------------------------------------------------------------------- drive download
DL = "https://drive.usercontent.google.com/download"


def client(routes, **kw):
    return DriveClient(session=FakeSession(routes), resolver=public_resolver, sleep=lambda s: None, log=lambda m: None, **kw)


def test_download_ok_with_name_from_header(tmp_path):
    r = FakeResponse(200, {"content-type": "application/pdf", "content-disposition": "attachment; filename=\"My Doc.pdf\""}, b"%PDF-1.4 data")
    d = client({DL: r})
    got = d.download("file", "ID1", str(tmp_path / "f"), 1000)
    assert got.name == "My Doc.pdf" and got.size == 13 and (tmp_path / "f").read_bytes().startswith(b"%PDF")
    assert r.closed


def test_download_size_cap(tmp_path):
    d = client({DL: FakeResponse(200, {"content-type": "video/mp4"}, b"x" * 5000)})
    with pytest.raises(DriveError) as e:
        d.download("file", "ID1", str(tmp_path / "f"), 1000)
    assert e.value.state == "too_large"
    d = client({DL: FakeResponse(200, {"content-type": "video/mp4", "content-length": "99999"}, b"x")})
    with pytest.raises(DriveError) as e:
        d.download("file", "ID1", str(tmp_path / "f"), 1000)
    assert e.value.state == "too_large"


def test_download_quota_html_retries_then_quota(tmp_path):
    slept = []
    sess = FakeSession({DL: FakeResponse(200, {"content-type": "text/html"}, b"Sorry, you can't view or download this file at this time. Too many users have viewed or downloaded this file recently")})
    d = DriveClient(session=sess, resolver=public_resolver, sleep=slept.append, log=lambda m: None, tries=3)
    with pytest.raises(DriveError) as e:
        d.download("file", "ID1", str(tmp_path / "f"), 1000)
    assert e.value.state == "quota" and slept == [2, 4] and len(sess.requests) == 3


def test_download_not_public(tmp_path):
    d = client({DL: FakeResponse(200, {"content-type": "text/html"}, b"<html>Sign in to continue</html>")})
    with pytest.raises(DriveError) as e:
        d.download("file", "ID1", str(tmp_path / "f"), 1000)
    assert e.value.state == "inaccessible"
    d = client({DL: FakeResponse(404)})
    with pytest.raises(DriveError) as e:
        d.download("file", "ID1", str(tmp_path / "f"), 1000)
    assert e.value.state == "inaccessible"


def test_download_virus_scan_form(tmp_path):
    page = (b'<form id="download-form" action="https://drive.usercontent.google.com/download" method="get">'
            b'<input type="hidden" name="id" value="ID1"><input type="hidden" name="confirm" value="t">'
            b'<input type="hidden" name="uuid" value="u-1"></form>')
    sess = FakeSession({DL: [FakeResponse(200, {"content-type": "text/html"}, page),
                             FakeResponse(200, {"content-type": "application/pdf"}, b"%PDF-ok")]})
    d = DriveClient(session=sess, resolver=public_resolver, sleep=lambda s: None, log=lambda m: None)
    got = d.download("file", "ID1", str(tmp_path / "f"), 1000, name_hint="a.pdf")
    assert got.name == "a.pdf" and sess.requests[1][1]["uuid"] == "u-1"


def test_redirect_off_allowlist_blocked(tmp_path):
    d = client({DL: FakeResponse(302, location="https://evil.example.com/x")})
    with pytest.raises(PipelineError, match="Host not allowed"):
        d.download("file", "ID1", str(tmp_path / "f"), 1000)
    d = client({DL: FakeResponse(302, location="http://doc-1.googleusercontent.com/x")})  # https only
    with pytest.raises(PipelineError):
        d.download("file", "ID1", str(tmp_path / "f"), 1000)


def test_redirect_to_googleusercontent_followed(tmp_path):
    d = client({DL: FakeResponse(302, location="https://doc-1.googleusercontent.com/x"),
                "https://doc-1.googleusercontent.com/": FakeResponse(200, {"content-type": "application/pdf"}, b"%PDF-1")})
    assert d.download("file", "I", str(tmp_path / "f"), 100).size == 6


def test_export_url_for_docs(tmp_path):
    sess = FakeSession({"https://docs.google.com/document/d/D1/export": FakeResponse(200, {"content-type": "application/vnd.openxmlformats"}, b"PK\x03\x04")})
    d = DriveClient(session=sess, resolver=public_resolver, sleep=lambda s: None, log=lambda m: None)
    got = d.download("document", "D1", str(tmp_path / "f"), 100)
    assert got.name == "document_D1.docx" and sess.requests[0][0].endswith("export?format=docx")


def test_network_error_retried(tmp_path):
    class Flaky(FakeSession):
        n = 0

        def get(self, url, params=None, **kw):
            Flaky.n += 1
            if Flaky.n < 3:
                raise OSError("connection reset")
            return FakeResponse(200, {"content-type": "application/pdf"}, b"%PDF-1")

    d = DriveClient(session=Flaky({}), resolver=public_resolver, sleep=lambda s: None, log=lambda m: None)
    assert d.download("file", "I", str(tmp_path / "f"), 100).size == 6


# ----------------------------------------------------------------------------- folder listing
def test_list_remote_folder_and_cap_warning():
    files = [(f"id{i}", f"f{i}.pdf") for i in range(49)] + [("idn", "sub/x.pdf")]
    d = DriveClient(session=FakeSession({}), list_folder=lambda fid: files, resolver=public_resolver,
                    sleep=lambda s: None, log=lambda m: None)
    top, nested, warns = d.list_remote("folder", "F")
    assert len(top) == 49 and nested == 1
    assert any("50" in w for w in warns) and any("subfolders" in w for w in warns)


def test_list_remote_private_folder_and_quota():
    def private(fid):
        raise RuntimeError("Cannot retrieve the public link of the file. You may need to change the permission to 'Anyone with the link'")

    d = DriveClient(session=FakeSession({}), list_folder=private, resolver=public_resolver, sleep=lambda s: None, log=lambda m: None)
    with pytest.raises(DriveError) as e:
        d.list_remote("folder", "F")
    assert e.value.state == "inaccessible"

    def quota(fid):
        raise RuntimeError("Too many users have viewed or downloaded this file recently")

    d = DriveClient(session=FakeSession({}), list_folder=quota, resolver=public_resolver, sleep=lambda s: None, log=lambda m: None)
    with pytest.raises(DriveError) as e:
        d.list_remote("folder", "F")
    assert e.value.state == "quota"


def test_open_link_falls_back_to_single_file():
    def boom(fid):
        raise RuntimeError("not a folder")

    d = DriveClient(session=FakeSession({}), list_folder=boom, resolver=public_resolver, sleep=lambda s: None, log=lambda m: None, tries=1)
    assert d.list_remote("open", "X")[0] == [("X", "")]


def test_error_code_mapping():
    assert code_from_error({"Error": "PipelineError", "Cause": '{"errorMessage": "drive_quota: busy"}'}) == ("drive_quota", "busy")
    assert code_from_error({"Error": "States.Timeout", "Cause": ""})[0] == "timeout"
    assert code_from_error({"Error": "X", "Cause": "boom"})[0] == "internal"
    assert drive_mod.EXPORTS["document"] == "docx"
