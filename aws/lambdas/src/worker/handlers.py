"""Lambda entry points (thin wrappers). Image config Command = "worker.handlers.<name>".

Some functions serve extra modes, selected by event["mode"]:
  assemble     "fail"          -> failure handler for the state machine Catch path (writes status.json / progress.json)
  assemble     "merge_video"   -> merge one video's transcript and frame notes (MergeVideo, after its Parallel branches)
  plan_audio   "plan_frames"   -> PlanFrames: sample one video's frames into contact sheets (needs the 10 GB /tmp)
  analyze_image "video_sheets" -> AnalyzeSheets: one vision request over a batch of a video's contact sheets
  extract_doc  "chat_extract"  -> text of a chat attachment (PDF / DOCX), invoked synchronously by the backend
"""
from __future__ import annotations

import json

from .store import S3Store


def _log(event: dict, name: str) -> None:
    print(json.dumps({"fn": name, "evaluation_id": event.get("evaluation_id"), "source_id": (event.get("file") or {}).get("source_id") or event.get("source_id")}))


def ingest(event, context=None):
    from .ingest import run_ingest

    _log(event, "ingest")
    return run_ingest(event, S3Store(event["bucket"]))


def extract_doc(event, context=None):
    if event.get("mode") == "chat_extract":
        from .chat_extract import run_chat_extract

        print(json.dumps({"fn": "extract_doc:chat_extract", "key": str(event.get("key"))[:200]}))
        return run_chat_extract(event)  # builds its store after checking the event; never raises
    from .docs import run_extract_doc

    _log(event, "extract_doc")
    return run_extract_doc(event, S3Store(event["bucket"]))


def plan_audio(event, context=None):
    if event.get("mode") == "plan_frames":
        from .frames import run_plan_frames

        _log(event, "plan_frames")
        return run_plan_frames(event, S3Store(event["bucket"]))
    from .audio import run_plan_audio

    _log(event, "plan_audio")
    return run_plan_audio(event, S3Store(event["bucket"]))


def transcribe_chunk(event, context=None):
    from .media import run_transcribe_chunk

    _log(event, "transcribe_chunk")
    return run_transcribe_chunk(event, S3Store(event["bucket"]))


def analyze_image(event, context=None):
    from .media import run_analyze_image

    _log(event, "analyze_sheets" if event.get("mode") == "video_sheets" else "analyze_image")
    return run_analyze_image(event, S3Store(event["bucket"]))


def assemble(event, context=None):
    from . import corpus

    store = S3Store(event["bucket"])
    mode = event.get("mode", "assemble")
    _log(event, f"assemble:{mode}")
    if mode == "fail":
        return corpus.run_fail(event, store)
    if mode == "merge_video":
        f = event["file"]
        corpus.merge_video(store, event["evaluation_id"], f["source_id"], f.get("original_name", ""))
        return {"source_id": f["source_id"]}
    return corpus.run_assemble(event, store)
