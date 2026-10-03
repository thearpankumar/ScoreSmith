#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["gdown>=5.2", "openpyxl>=3.1", "requests>=2.31"]
# ///
"""Download each student's public Google Drive submission into GdriveDownload/<email-id>/.

No Google API key / OAuth is needed: links are public, and `gdown` scrapes the
public folder page (listing) and the public download endpoint (files).

Usage (uv installs the dependencies automatically):
    uv run scripts/gdrive_download.py                       # incremental: only NEW files
    uv run scripts/gdrive_download.py --replace             # also overwrite files that changed
    uv run scripts/gdrive_download.py --force               # re-download everything
    uv run scripts/gdrive_download.py --only am8131 --dry-run
    uv run scripts/gdrive_download.py --clean               # wipe the whole GdriveDownload folder

Behaviour
  * Only top-level files of each Drive folder are fetched (subfolders ignored).
  * Folder name = local part of the email (am8131@srmist.edu.in -> am8131).
  * A per-student `.manifest.json` remembers Drive file id -> local name/size.
      - id known and file intact on disk          -> skipped
      - new name / new id                         -> downloaded
      - same name but a *different* Drive id      -> student re-uploaded; kept as-is by
        default, overwritten with --replace
  * Downloads go to `<name>.part` first and are moved into place atomically, so an
    interrupted run never leaves a half-written file looking complete.
  * If a student submitted more than once, the latest row (by Timestamp) wins.
  * Supports folder links, single-file links and Google Docs/Sheets/Slides links
    (exported to docx/xlsx/pptx).

Limitations (inherent to key-less access)
  * A file edited in place keeps its Drive id, so --replace cannot see it; use --force.
  * Google caps a folder listing at 50 files and may throttle with a "quota exceeded"
    page for very popular files; those students are reported as failed - re-run later.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import gdown
import openpyxl
import requests

HERE = Path(__file__).resolve().parent
MANIFEST = ".manifest.json"
EXPORTS = {  # Google-native doc type -> export format
    "document": "docx",
    "spreadsheets": "xlsx",
    "presentation": "pptx",
}
WANTED = (".pdf", ".docx", ".mp4")  # only used for the end-of-run report; all top-level files are fetched
_print_lock = threading.Lock()
STOP = threading.Event()  # set by Ctrl+C: finish the current file, start no new ones


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


# --------------------------------------------------------------------------- input
def parse_link(url: str) -> tuple[str, str] | None:
    """Return (kind, id) where kind is folder | file | document | spreadsheets | presentation."""
    u = urlparse(url.strip())
    m = re.search(r"/folders/([\w-]+)", u.path)
    if m:
        return "folder", m.group(1)
    m = re.search(r"/(document|spreadsheets|presentation)/d/([\w-]+)", u.path)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"/file/d/([\w-]+)", u.path)
    if m:
        return "file", m.group(1)
    q = parse_qs(u.query).get("id")
    if q:  # open?id=... (could be file or folder; folder listing is tried first)
        return "open", q[0]
    return None


def find_col(header: list, *needles: str) -> int:
    for i, h in enumerate(header):
        if h and any(n in str(h).lower() for n in needles):
            return i
    raise SystemExit(f"Could not find a column matching {needles} in header {header}")


def read_submissions(xlsx: Path) -> dict[str, dict]:
    """email-id -> {url, email, name} from the latest submission."""
    ws = openpyxl.load_workbook(xlsx).active
    rows = list(ws.iter_rows())
    header = [c.value for c in rows[0]]
    c_email, c_url = find_col(header, "email"), find_col(header, "drive", "url", "link")
    c_ts = next((i for i, h in enumerate(header) if h and "timestamp" in str(h).lower()), None)

    c_name = next((i for i, h in enumerate(header) if h and str(h).strip().lower() == "name"), None)

    latest: dict[str, tuple[object, dict]] = {}
    for n, row in enumerate(rows[1:], start=2):
        email = row[c_email].value
        cell = row[c_url]
        url = str(cell.value or "").strip()
        if not url.startswith("http") and cell.hyperlink:
            url = cell.hyperlink.target or ""
        if not email or "@" not in str(email):
            continue
        if not url.startswith("http"):
            log(f"[warn] row {n}: {email} has no usable URL ({url!r})")
            continue
        sid = str(email).strip().lower().split("@")[0]
        ts = row[c_ts].value if c_ts is not None and row[c_ts].value else n
        try:
            newer = sid not in latest or ts >= latest[sid][0]
        except TypeError:  # mixed timestamp / row-number comparison
            newer = True
        if newer:
            name = str(row[c_name].value or "").strip() if c_name is not None else ""
            latest[sid] = (ts, dict(url=url, email=str(email).strip(), name=name))
    return {k: v[1] for k, v in latest.items()}


# --------------------------------------------------------------------------- helpers
def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "unnamed"


def load_manifest(d: Path) -> dict:
    try:
        return json.loads((d / MANIFEST).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(d: Path, m: dict) -> None:
    tmp = d / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(m, indent=2, ensure_ascii=False), "utf-8")
    os.replace(tmp, d / MANIFEST)


def is_permission_error(e: Exception) -> bool:
    return "public link" in str(e) or "Anyone with the link" in str(e) or "not public" in str(e)


def short_err(e: Exception) -> str:
    if is_permission_error(e):
        return "not public (needs 'Anyone with the link') or Drive quota/rate limit hit - retry later"
    return f"{type(e).__name__}: {' '.join(str(e).split())[:100]}"


def retry(fn, tries: int, what: str):
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as e:  # gdown raises several unrelated exception types
            if attempt == tries or is_permission_error(e):  # retrying a private link is pointless
                raise
            wait = 2**attempt
            log(f"    retry {attempt}/{tries - 1} for {what} in {wait}s ({short_err(e)})")
            time.sleep(wait)


def fetch(file_id: str, dest: Path, tries: int) -> None:
    """Download one public file id to dest (via .part + atomic rename)."""
    part = dest.with_name(dest.name + ".part")

    def go():
        part.unlink(missing_ok=True)
        got = gdown.download(id=file_id, output=str(part), quiet=True, resume=False)
        if not got or not part.exists() or part.stat().st_size == 0:
            raise RuntimeError("empty or blocked download (not public / quota exceeded?)")

    try:
        retry(go, tries, dest.name)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


def fetch_export(kind: str, doc_id: str, dest: Path, tries: int) -> None:
    fmt = EXPORTS[kind]
    url = f"https://docs.google.com/{kind}/d/{doc_id}/export?format={fmt}"
    part = dest.with_name(dest.name + ".part")

    def go():
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            if "text/html" in r.headers.get("content-type", ""):
                raise RuntimeError("got an HTML page instead of a file (not public?)")
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)

    try:
        retry(go, tries, dest.name)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)


def doc_title(kind: str, doc_id: str) -> str:
    """Best-effort title of a Google Doc, from the Content-Disposition of the export."""
    url = f"https://docs.google.com/{kind}/d/{doc_id}/export?format={EXPORTS[kind]}"
    try:
        r = requests.get(url, stream=True, timeout=30)
        cd = r.headers.get("content-disposition", "")
        r.close()
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
        if m:
            from urllib.parse import unquote

            return unquote(m.group(1))
        m = re.search(r'filename="([^"]+)"', cd)
        if m:
            return m.group(1)
    except requests.RequestException:
        pass
    return f"{kind}_{doc_id}.{EXPORTS[kind]}"


# --------------------------------------------------------------------------- core
def list_remote(kind: str, rid: str, tries: int) -> tuple[list[tuple[str, str]], int]:
    """Return ([(drive_id, filename)] of top-level files, number of files hidden in subfolders)."""
    if kind in ("folder", "open"):
        try:
            files = retry(
                lambda: gdown.download_folder(id=rid, skip_download=True, quiet=True, use_cookies=False),
                tries, "folder listing",
            )
        except Exception:
            if kind == "folder":
                raise
            files = None  # open?id= pointing at a single file
        if files is not None:
            top = [(f.id, f.path) for f in files if "/" not in f.path and "\\" not in f.path]
            return top, len(files) - len(top)
    # single file: name is learned when downloaded
    return [(rid, "")], 0


def process(sid: str, url: str, out: Path, args) -> dict:
    stats = dict(new=0, replaced=0, skipped=0, failed=0, issue=None, inaccessible=[], aborted=False)
    parsed = parse_link(url)
    if not parsed:
        log(f"[{sid}] FAIL unrecognised link: {url}")
        stats["inaccessible"].append("unrecognised link format")
        stats["failed"] += 1
        return stats
    kind, rid = parsed
    sdir = out / sid
    log(f"[{sid}] {kind} {rid}")

    try:
        if kind in EXPORTS:
            remote, nested = [(rid, safe_name(doc_title(kind, rid)))], 0
        else:
            remote, nested = list_remote(kind, rid, args.retries)
    except Exception as e:
        log(f"[{sid}] FAIL listing: {short_err(e)}")
        stats["inaccessible"].append(short_err(e))
        stats["failed"] += 1
        return stats

    if args.ext:  # e.g. --ext pdf docx : skip videos etc. (named files only; single-file links are kept)
        remote = [(i, n) for i, n in remote if not n or Path(n).suffix.lower().lstrip(".") in args.ext]

    # Report students whose top level has nothing we want (subfolders are never entered).
    if not remote:
        stats["issue"] = (
            f"top level has no files ({nested} file(s) only inside subfolders, not downloaded)"
            if nested else "folder is empty"
        )
        log(f"[{sid}] NOTHING: {stats['issue']}")
        return stats
    named = [n for _, n in remote if n]
    if named and not any(n.lower().endswith(WANTED) for n in named):
        stats["issue"] = "no pdf/docx/mp4 at top level (found: " + ", ".join(named[:5]) + ")"
        log(f"[{sid}] WARNING: {stats['issue']}")
    if nested:
        log(f"[{sid}] note: ignored {nested} file(s) inside subfolders")

    if not args.dry_run:
        sdir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(sdir)
    by_name = {v["name"]: k for k, v in manifest.items()}
    used_names: set[str] = set()

    for fid, raw in remote:
        if STOP.is_set():
            stats["aborted"] = True
            log(f"[{sid}] stopped before remaining files")
            break
        name = safe_name(raw) if raw else ""
        # --- single-file link: learn the name by downloading into a scratch dir
        if not name:
            known = manifest.get(fid)
            if known and (sdir / known["name"]).exists() and not args.force:
                log(f"[{sid}]  = {known['name']} (unchanged)")
                stats["skipped"] += 1
                continue
            if args.dry_run:
                log(f"[{sid}]  + <single file {fid}> (would download)")
                stats["new"] += 1
                continue
            if not (args.replace or args.force):  # learn the name cheaply; skip if we already have that file
                try:
                    probe = gdown.download(id=fid, output=str(sdir) + os.sep, quiet=True, skip_download=True)
                    pname = safe_name(Path(getattr(probe, "local_path", "") or "").name)
                    if pname and (sdir / pname).exists() and (sdir / pname).stat().st_size > 0:
                        manifest.setdefault(fid, {"name": pname, "size": (sdir / pname).stat().st_size})
                        log(f"[{sid}]  = {pname} (already on disk)")
                        stats["skipped"] += 1
                        save_manifest(sdir, manifest)
                        continue
                except Exception:
                    pass  # fall through to the normal download path
            tmp = sdir / f".tmp_{fid}"
            tmp.mkdir(exist_ok=True)
            try:
                got = retry(lambda: gdown.download(id=fid, output=str(tmp) + os.sep, quiet=True), args.retries, fid)
                if not got:
                    raise RuntimeError("blocked download (not public / quota exceeded?)")
                name = safe_name(Path(got).name)
                target = sdir / name
                existed = target.exists()
                if existed and not (args.replace or args.force) and manifest.get(fid, {}).get("name") != name:
                    log(f"[{sid}]  ~ {name} exists with different id; kept (use --replace)")
                    stats["skipped"] += 1
                    continue
                os.replace(got, target)
                manifest[fid] = {"name": name, "size": target.stat().st_size}
                log(f"[{sid}]  {'↻' if existed else '+'} {name}")
                stats["replaced" if existed else "new"] += 1
            except Exception as e:
                log(f"[{sid}]  ! {fid}: {short_err(e)}")
                stats["inaccessible"].append(f"file {fid}: {short_err(e)}")
                stats["failed"] += 1
            finally:
                for p in tmp.glob("*"):
                    p.unlink(missing_ok=True)
                tmp.rmdir()
            save_manifest(sdir, manifest)
            continue

        # --- normal file with known name; disambiguate duplicate names in one folder
        if name in used_names:
            stem, ext = os.path.splitext(name)
            name = f"{stem} ({fid[:6]}){ext}"
        used_names.add(name)
        target = sdir / name
        known = manifest.get(fid)
        on_disk = target.exists() and target.stat().st_size > 0

        recorded_name = by_name.get(name)  # drive id we previously saved under this name
        if args.force:
            action = "replace" if on_disk else "new"
        elif not on_disk:
            action = "new"
        elif known and known["name"] == name:
            action = "skip"  # same Drive id, already downloaded
        elif recorded_name is None:
            action = "adopt"  # file already there but never recorded (manual copy / older run)
        else:
            # same name but a different Drive id: the student re-uploaded it
            action = "replace" if args.replace else "keep"

        if action == "skip":
            stats["skipped"] += 1
            continue
        if action == "adopt":
            manifest[fid] = {"name": name, "size": target.stat().st_size}
            log(f"[{sid}]  = {name} (already on disk)")
            stats["skipped"] += 1
            continue
        if action == "keep":
            log(f"[{sid}]  ~ {name}: already present, re-uploaded version not downloaded (use --replace)")
            stats["skipped"] += 1
            continue
        if args.dry_run:
            log(f"[{sid}]  {'↻' if action == 'replace' else '+'} {name} (would {'overwrite' if action == 'replace' else 'download'})")
            stats["replaced" if action == "replace" else "new"] += 1
            continue
        try:
            if kind in EXPORTS:
                fetch_export(kind, fid, target, args.retries)
            else:
                fetch(fid, target, args.retries)
            # drop the old manifest entry that pointed at this name (replaced by new id)
            old = by_name.get(name)
            if old and old != fid:
                manifest.pop(old, None)
            manifest[fid] = {"name": name, "size": target.stat().st_size}
            by_name[name] = fid
            log(f"[{sid}]  {'↻' if action == 'replace' else '+'} {name}")
            stats["replaced" if action == "replace" else "new"] += 1
        except Exception as e:
            log(f"[{sid}]  ! {name}: {short_err(e)}")
            stats["inaccessible"].append(f"{name}: {short_err(e)}")
            stats["failed"] += 1
        save_manifest(sdir, manifest)

    if not args.dry_run and sdir.exists():
        save_manifest(sdir, manifest)
    return stats


def render_summary(state: dict) -> str:
    empty = {k: v for k, v in state.items() if v["empty"]}
    blocked = {k: v for k, v in state.items() if v["inaccessible"]}
    lines = [f"Last updated: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    lines.append(f"1. FOLDERS WITH NO USABLE FILES AT TOP LEVEL ({len(empty)})")
    lines += [f"   {v['label']}  [{v['empty']}]" for v in empty.values()] or ["   (none)"]
    lines += ["", f"2. LINKS NOT ACCESSIBLE ({len(blocked)})"]
    for v in blocked.values():
        lines.append(f"   {v['label']}  {v['url']}")
        lines += [f"       - {r}" for r in dict.fromkeys(v["inaccessible"])]
    if not blocked:
        lines.append("   (none)")
    return "\n".join(lines) + "\n"


def summary_entry(info: dict, st: dict) -> dict | None:
    if not (st["issue"] or st["inaccessible"]):
        return None
    return dict(
        label=f"{info['email']} - {info['name'] or '(no name)'}",
        url=info["url"], empty=st["issue"], inaccessible=st["inaccessible"],
    )


def write_shard_summary(out: Path, n: int, entries: dict) -> None:
    """Each worker thread keeps its own summary-<n>.txt up to date as it finishes students."""
    out.mkdir(parents=True, exist_ok=True)
    (out / f"summary-{n}.txt").write_text(f"Worker {n} (partial)\n" + render_summary(entries), "utf-8")


def merge_summaries(out: Path, subs: dict[str, dict], results: dict[str, dict], dry_run: bool) -> Path | None:
    """Combine all worker results into the single summary.txt and delete summary-<n>.txt.
    State persists across runs, so a partial run (--only) only refreshes the students it
    processed and a student drops off once resolved."""
    if dry_run:
        return None
    out.mkdir(parents=True, exist_ok=True)
    state_file = out / ".summary_state.json"
    try:
        state = json.loads(state_file.read_text("utf-8"))
    except (OSError, ValueError):
        state = {}
    for sid, st in results.items():
        state.pop(sid, None)
        entry = summary_entry(subs[sid], st)
        if entry:
            state[sid] = entry
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), "utf-8")
    path = out / "summary.txt"
    path.write_text(render_summary(state), "utf-8")
    for part in out.glob("summary-*.txt"):
        part.unlink(missing_ok=True)
    return path


def clean(out: Path, yes: bool, dry_run: bool) -> int:
    """Wipe the output folder. Refuses obviously dangerous targets."""
    out = out.resolve()
    unsafe = {Path(out.anchor), Path.home().resolve(), HERE, Path.cwd().resolve()}
    if out in unsafe or out in HERE.parents or out in Path.cwd().resolve().parents:
        log(f"Refusing to delete {out}: not a dedicated download folder.")
        return 2
    if not out.exists():
        log(f"Nothing to clean: {out} does not exist.")
        return 0
    n_dirs = sum(1 for p in out.iterdir() if p.is_dir())
    n_files = sum(1 for p in out.rglob("*") if p.is_file())
    size_mb = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e6
    log(f"{out}: {n_dirs} student folders, {n_files} files, {size_mb:,.1f} MB")
    if dry_run:
        log("[dry-run] nothing deleted")
        return 0
    if not yes and input("Delete all of it? Type 'yes' to confirm: ").strip().lower() != "yes":
        log("Aborted.")
        return 1
    shutil.rmtree(out)
    log("Deleted.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xlsx", type=Path, help="submission sheet (default: first .xlsx in scripts/SampleFiles)")
    ap.add_argument("--out", type=Path, default=HERE / "GdriveDownload", help="output root (default: scripts/GdriveDownload)")
    ap.add_argument("--replace", action="store_true", help="overwrite local files when the student re-uploaded a file under the same name")
    ap.add_argument("--force", action="store_true", help="re-download every file, even unchanged ones")
    ap.add_argument("--only", nargs="+", metavar="ID", help="only these email ids (e.g. am8131) ")
    ap.add_argument("--ext", nargs="+", metavar="EXT", type=str.lower, help="only download files with these extensions (e.g. pdf docx)")
    ap.add_argument("--clean", action="store_true", help="delete the entire output folder (all downloads, manifests, summary) and exit")
    ap.add_argument("--yes", action="store_true", help="with --clean: skip the confirmation prompt")
    ap.add_argument("--dry-run", action="store_true", help="show what would happen, download nothing")
    ap.add_argument("--workers", type=int, default=5, help="concurrent worker threads (default 5)")
    ap.add_argument("--retries", type=int, default=3, help="attempts per network operation (default 3)")
    args = ap.parse_args()

    if args.clean:
        return clean(args.out, args.yes, args.dry_run)

    xlsx = args.xlsx or next(iter(sorted((HERE / "SampleFiles").glob("*.xlsx"))), None)
    if not xlsx or not xlsx.exists():
        raise SystemExit("No spreadsheet found; pass --xlsx PATH")

    subs = read_submissions(xlsx)
    if args.only:
        want = {s.lower().split("@")[0] for s in args.only}
        subs = {k: v for k, v in subs.items() if k in want}
    log(f"{len(subs)} students from {xlsx.name} -> {args.out}"
        f"{'  [dry-run]' if args.dry_run else ''}{'  [replace]' if args.replace else ''}{'  [force]' if args.force else ''}")

    total = dict(new=0, replaced=0, skipped=0, failed=0)
    failed_students: list[str] = []
    results: dict[str, dict] = {}
    work: queue.Queue = queue.Queue()
    for sid in subs:
        work.put(sid)
    lock = threading.Lock()
    n_workers = max(1, min(args.workers, len(subs) or 1))

    def worker(n: int) -> None:
        mine: dict[str, dict] = {}  # this worker's own summary entries
        while not STOP.is_set():
            try:
                sid = work.get_nowait()
            except queue.Empty:
                break
            try:
                st = process(sid, subs[sid]["url"], args.out, args)
            except Exception as e:
                log(f"[{sid}] CRASH {type(e).__name__}: {e}")
                st = dict(new=0, replaced=0, skipped=0, failed=1, issue=None, inaccessible=[f"crash: {e}"])
            if st.get("aborted"):  # interrupted mid-student: leave its previous summary state alone
                continue
            with lock:
                results[sid] = st
                for k in total:
                    total[k] += st[k]
                if st["failed"]:
                    failed_students.append(sid)
            entry = summary_entry(subs[sid], st)
            if entry:
                mine[sid] = entry
            if not args.dry_run:
                write_shard_summary(args.out, n, mine)

    threads = [threading.Thread(target=worker, args=(n,), name=f"worker-{n}", daemon=True) for n in range(1, n_workers + 1)]
    for t in threads:
        t.start()

    def wait_all() -> None:
        while any(t.is_alive() for t in threads):  # short joins so Ctrl+C is delivered on Windows too
            for t in threads:
                t.join(0.2)

    interrupted = False
    try:
        wait_all()
    except KeyboardInterrupt:
        interrupted = True
        STOP.set()
        log("\nCtrl+C: finishing files in progress, then saving the summary... (press Ctrl+C again to quit immediately)")
        try:
            wait_all()
        except KeyboardInterrupt:
            log("Forced quit.")
    # a hard quit can leave half-written downloads; they are never promoted, just remove them
    if interrupted and not args.dry_run:
        for part in args.out.glob("*/*.part"):
            part.unlink(missing_ok=True)
        for tmp in args.out.glob("*/.tmp_*"):
            shutil.rmtree(tmp, ignore_errors=True)

    log(f"\nDone: {total['new']} new, {total['replaced']} replaced, {total['skipped']} skipped, {total['failed']} failed")
    if failed_students:
        log("Failed (re-run to retry only missing files): " + ", ".join(sorted(failed_students)))
    with lock:
        path = merge_summaries(args.out, subs, dict(results), args.dry_run)
    if path:
        log(f"Summary updated: {path}")
    if interrupted:
        log("Interrupted - re-run the script to continue; finished files are skipped.")
        return 130
    return 1 if failed_students else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # Ctrl+C outside the download phase (reading the sheet, --clean prompt)
        print("\nAborted.")
        sys.exit(130)
