"""Upload key scoping and validation helpers (app/pipeline/uploads.py): a key is only ever accepted for the user
whose folder it lives in, for the purpose it was issued for, with an extension we allow and no path tricks."""

from __future__ import annotations

import uuid

import pytest

from app.pipeline import uploads as up

U = uuid.uuid4()
OTHER = uuid.uuid4()
G = uuid.uuid4()
F = uuid.uuid4()


def test_built_keys_roundtrip_through_validation() -> None:
    sub = up.build_key("submission", U, G, "pdf")
    sheet = up.build_key("batch_sheet", U, G, "xlsx")
    assert sub.startswith(f"uploads/{U}/{G}/") and sheet.startswith(f"batches/{U}/{G}/")
    assert up.key_extension(sub, U, "submission") == "pdf"
    assert up.key_extension(sheet, U, "batch_sheet") == "xlsx"
    assert up.key_extension(sub) == "pdf" and up.key_extension(sheet) == "xlsx"  # purpose=None: either layout
    assert up.build_key("submission", U, G, "pdf") != sub  # a fresh object id every time


def test_other_users_folder_is_rejected_for_every_purpose() -> None:
    sub = up.build_key("submission", U, G, "pdf")
    sheet = up.build_key("batch_sheet", U, G, "csv")
    for purpose in ("submission", "batch_sheet", None):
        assert up.key_extension(sub, OTHER, purpose) is None
        assert up.key_extension(sheet, OTHER, purpose) is None


def test_purpose_mismatch_is_rejected() -> None:
    sub = up.build_key("submission", U, G, "pdf")
    sheet = up.build_key("batch_sheet", U, G, "xlsx")
    assert up.key_extension(sub, U, "batch_sheet") is None
    assert up.key_extension(sheet, U, "submission") is None


@pytest.mark.parametrize(
    "key",
    [
        "",
        "uploads",
        f"uploads/{U}/{G}/{F}",  # no extension
        f"uploads/{U}/{G}/{F}.",  # empty extension
        f"uploads/{U}/{G}/{F}.exe",  # extension not allowed
        f"uploads/{U}/{G}/{F}.xlsx",  # batch extension under the submission prefix
        f"uploads/{U}/{G}/{F}.PDF",  # case-sensitive: we only ever issue lowercase
        f"uploads/{U}/{G}/{F}.pdf/extra",
        f"uploads/{U}/../{OTHER}/{G}/{F}.pdf",  # traversal
        f"uploads/{U}/{G}/../{F}.pdf",
        f"uploads//{U}/{G}/{F}.pdf",
        f"/uploads/{U}/{G}/{F}.pdf",  # leading slash
        f"uploads/{U}/{G}/{F}.pdf\n",  # trailing newline must not slip past the regex
        f"uploads/{U}/{G}/{F}.pdf%00.exe",
        f"uploads/{str(U).upper()}/{G}/{F}.pdf",  # uppercase uuid is not what we issue
        f"uploads/not-a-uuid/{G}/{F}.pdf",
        f"other/{U}/{G}/{F}.pdf",
        f"s3://bucket/uploads/{U}/{G}/{F}.pdf",
        f"batches/{U}/{G}/{F}.pdf",  # submission extension under the batches prefix
    ],
)
def test_malformed_or_hostile_keys_are_never_accepted(key: str) -> None:
    for purpose in ("submission", "batch_sheet", None):
        assert up.key_extension(key, U, purpose) is None, (key, purpose)
        assert up.key_extension(key, None, purpose) is None, (key, purpose)


@pytest.mark.parametrize(
    ("name", "ext"),
    [("report.PDF", "pdf"), ("a.b.docx", "docx"), ("noext", ""), ("x.", ""), ("архив.Mp4", "mp4")],
)
def test_file_extension_is_lowercased_last_suffix(name: str, ext: str) -> None:
    assert up.file_extension(name) == ext


def test_allowed_extensions_per_purpose_are_disjoint() -> None:
    assert not (up.allowed_extensions("submission") & up.allowed_extensions("batch_sheet"))
    assert "exe" not in up.allowed_extensions("submission") and "html" not in up.allowed_extensions("submission")


def test_part_sizing_respects_s3_limits() -> None:
    five_mib = 5 * 1024 * 1024
    assert up.part_size_for(1, 1) == five_mib  # never below S3's 5 MiB minimum
    assert up.part_size_for(five_mib * 3, five_mib * 2) == five_mib * 2
    huge = 2 * 1024**3
    size = up.part_size_for(huge, five_mib)
    assert up.part_count(huge, size) <= up.MAX_PARTS
    giant = 5 * 1024**4
    assert up.part_count(giant, up.part_size_for(giant, five_mib)) <= up.MAX_PARTS
    assert up.part_count(0, five_mib) == 1 and up.part_count(five_mib, five_mib) == 1
    assert up.part_count(five_mib + 1, five_mib) == 2


@pytest.mark.parametrize(
    ("ext", "head", "ok"),
    [
        ("pdf", b"%PDF-1.7", True),
        ("pdf", b"MZ\x90\x00", False),  # an executable renamed to .pdf
        ("pdf", b"", False),
        ("docx", b"PK\x03\x04", True),
        ("docx", b"%PDF", False),
        ("xlsx", b"PK\x03\x04rest", True),
        ("mp4", b"\x00\x00\x00\x18ftypmp42", True),
        ("mov", b"\x00\x00\x00\x14ftypqt  ", True),
        ("mp4", b"%PDF-1.7 not a video", False),
        ("mp4", b"short", False),
        ("webm", b"\x1a\x45\xdf\xa3", True),
        ("mkv", b"\x1a\x45\xdf\xa3\x01", True),
        ("webm", b"RIFF", False),
        ("txt", b"hello world", True),
        ("txt", b"bin\x00ary", False),
        ("csv", b"a,b\n1,2", True),
        ("csv", b"", False),
        ("md", b"# title", True),
        ("exe", b"MZ", False),  # unknown extensions never validate
    ],
)
def test_magic_bytes_sniffing(ext: str, head: bytes, ok: bool) -> None:
    assert up.magic_ok(ext, head) is ok
