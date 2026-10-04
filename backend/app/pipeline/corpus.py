"""Loads the assembled corpus (`derived/{evaluation_id}/corpus.json`, see docs/ai-eval-contract.md)
and provides helpers to budget / render / validate against its labelled sections."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.pipeline.aws_jobs import AwsJobsProtocol, derived_key, read_corpus_json

MIN_USEFUL_CHARS = 20


@dataclass(frozen=True)
class Section:
    id: str
    source_id: str
    source_name: str
    kind: str  # doc | video | image
    label: str
    text: str


@dataclass
class Corpus:
    evaluation_id: str
    sections: list[Section] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    # Videos of the submission and what could be analysed of them: [{"name", "duration_sec", "has_audio",
    # "transcribed"}]. Read from the extraction step's own files (plan.json), not from the sections.
    media: list[dict[str, Any]] = field(default_factory=list)

    def has_unanalysed_video(self) -> bool:
        """A video whose audio could not be transcribed (silent or failed): what it shows or says is unknown."""
        return any(not m.get("transcribed") for m in self.media)

    def media_note(self) -> str:
        """A short plain-text description of the submission's videos for the prompts ('' when there are none), so
        the model knows a video's length and that it could not be analysed instead of guessing from the text."""
        if not self.media:
            return ""
        lines = []
        for m in self.media:
            seconds = m.get("duration_sec")
            if isinstance(seconds, int | float):
                length = f"{int(seconds) // 60}:{int(seconds) % 60:02d} long"
            else:
                length = "unknown length"
            if m.get("transcribed"):
                how = "its audio was transcribed, but what it shows on screen was NOT analysed"
            elif m.get("has_audio") is False:
                how = "it has no audio track (silent), so neither what it says nor what it shows could be analysed"
            else:
                how = "it could not be transcribed or analysed"
            lines.append(f"- {m.get('name') or 'video'}: video, {length}; {how}.")
        return "Submission media:\n" + "\n".join(lines)

    @property
    def total_chars(self) -> int:
        return sum(len(s.text) for s in self.sections)

    @property
    def is_empty(self) -> bool:
        """No extractable content: no section carries a meaningful amount of text."""
        return not any(len(s.text.strip()) >= MIN_USEFUL_CHARS for s in self.sections)

    def by_id(self) -> dict[str, Section]:
        return {s.id: s for s in self.sections}


def parse_corpus(data: dict[str, Any], evaluation_id: str = "") -> Corpus:
    sections: list[Section] = []
    for i, raw in enumerate(data.get("sections") or []):
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "")
        sections.append(
            Section(
                id=str(raw.get("id") or f"s{i + 1:03d}"),
                source_id=str(raw.get("source_id") or ""),
                source_name=str(raw.get("source_name") or ""),
                kind=str(raw.get("kind") or "doc"),
                label=str(raw.get("label") or raw.get("source_name") or f"SECTION {i + 1}"),
                text=text,
            )
        )
    stats = {k: int(v) for k, v in (data.get("stats") or {}).items() if isinstance(v, int | float)}
    return Corpus(
        evaluation_id=str(data.get("evaluation_id") or evaluation_id),
        sections=sections,
        stats=stats,
        warnings=[str(w) for w in (data.get("warnings") or [])],
    )


def load_corpus(aws: AwsJobsProtocol, evaluation_id: str) -> Corpus | None:
    data = read_corpus_json(aws, str(evaluation_id))
    if data is None:
        return None
    corpus = parse_corpus(data, str(evaluation_id))
    corpus.media = read_media(aws, str(evaluation_id), corpus)
    return corpus


def read_media(aws: AwsJobsProtocol, evaluation_id: str, corpus: Corpus) -> list[dict[str, Any]]:
    """Video facts (length, silent or not, transcribed or not) from the manifest and each video's plan.json.
    Best effort: anything missing just means less context, never a failure."""
    try:
        manifest = aws.read_json(derived_key(evaluation_id, "manifest.json")) or {}
        media: list[dict[str, Any]] = []
        for f in manifest.get("files") or []:
            if f.get("kind") != "video" or f.get("status") != "ok":
                continue
            sid = str(f.get("source_id") or "")
            plan = aws.read_json(derived_key(evaluation_id, f"video/{sid}/plan.json")) or {}
            transcribed = any(s.kind == "video" and s.source_id == sid for s in corpus.sections)
            media.append(
                {
                    "name": f.get("original_name") or plan.get("source") or "video",
                    "duration_sec": plan.get("duration_sec"),
                    "has_audio": plan.get("has_audio"),
                    "transcribed": transcribed,
                }
            )
        return media
    except Exception:  # noqa: BLE001 - media facts are optional context
        return []


def render_section(section: Section, max_chars: int | None = None) -> str:
    text = section.text if max_chars is None or len(section.text) <= max_chars else section.text[:max_chars] + " ..."
    return f"[{section.id}] {section.label}\n{text}"


def render_sections(sections: list[Section], budget_chars: int, per_section_cap: int | None = None) -> str:
    """Renders sections in order until `budget_chars` is used; the remainder is shared evenly so
    every section gets at least a prefix rather than later ones being dropped."""
    if not sections:
        return ""
    total = sum(len(s.text) + len(s.label) + 12 for s in sections)
    if total <= budget_chars and per_section_cap is None:
        return "\n\n".join(render_section(s) for s in sections)
    share = max(200, budget_chars // len(sections))
    if per_section_cap is not None:
        share = min(share, per_section_cap)
    out: list[str] = []
    used = 0
    for s in sections:
        piece = render_section(s, share)
        if used + len(piece) > budget_chars:
            break
        out.append(piece)
        used += len(piece) + 2
    return "\n\n".join(out)


def chunk_sections(sections: list[Section], max_chars: int) -> list[list[Section]]:
    """Groups consecutive sections into batches of at most ~`max_chars` characters each."""
    batches: list[list[Section]] = []
    cur: list[Section] = []
    size = 0
    for s in sections:
        n = min(len(s.text), max_chars) + len(s.label) + 12
        if cur and size + n > max_chars:
            batches.append(cur)
            cur, size = [], 0
        cur.append(s)
        size += n
    if cur:
        batches.append(cur)
    return batches


_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def is_verbatim(snippet: str, haystack: str) -> bool:
    """Whether `snippet` occurs verbatim in `haystack` (whitespace-insensitive, case-sensitive)."""
    needle = normalize_ws(snippet)
    return bool(needle) and needle in normalize_ws(haystack)
