"""A video's timeline in the corpus: what was said (transcript) and what was shown (frame notes), by time.

Pure functions only (no S3), used by PlanFrames (timestamps printed on the frames), AnalyzeSheets (frame lists in the
prompt) and merge_video / assemble (the VIDEO sections). Line format of a VIDEO section:

    [mm:ss] SAID: <transcript text spoken from that time>
    [mm:ss] SHOWN: <what the frame at that time shows>

Bedrock runs keep Amazon Transcribe's sentence times; OpenRouter transcripts have one SAID line per audio chunk, at
the chunk's start.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.\d+)?$")
# "[mm:ss] SHOWN: text", tolerating list markers and Markdown emphasis around the timestamp and the label
_SHOWN_RE = re.compile(r"^[\s>*_`-]*\[\s*((?:\d+:)?\d{1,2}:\d{2})\s*\][\s*_`]*SHOWN[\s*_`]*:"
                       r"(?:\s*[*_`]{1,3}(?=\s))?\s*(.*\S)\s*$", re.I)
_HEADINGS = (
    ("steps", re.compile(r"^[\s#*_`>-]*steps observed\b[^:]*:?[\s*_`]*(.*)$", re.I)),
    ("quality", re.compile(r"^[\s#*_`>-]*visual quality\b[^:]*:?[\s*_`]*(.*)$", re.I)),
    ("not_visible", re.compile(r"^[\s#*_`>-]*not visible in (?:the )?sampled frames\b[^:]*:?[\s*_`]*(.*)$", re.I)),
)
# a section body longer than this is split at line boundaries (the scorer quotes from sections; keep them readable)
MAX_WINDOW_CHARS = 9000


def fmt_ts(sec: float) -> str:
    sec = int(round(max(0.0, float(sec))))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def parse_ts(text: str) -> float | None:
    m = _TS_RE.match(text.strip())
    if not m:
        return None
    h, mm, ss = int(m.group(1) or 0), int(m.group(2)), int(m.group(3))
    return float(h * 3600 + mm * 60 + ss) if ss < 60 else None


# --------------------------------------------------------------------------- frame notes (AnalyzeSheets answers)
@dataclass
class Notes:
    shown: list[tuple[float, str]] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    quality: list[str] = field(default_factory=list)
    not_visible: list[str] = field(default_factory=list)


def _clean_item(line: str) -> str:
    return line.strip().strip("*_`").strip()


def parse_notes(text: str, frame_times: list[float] | None = None) -> Notes:
    """Split a vision model's answer into SHOWN lines and the three summary blocks. A SHOWN time that is not near any
    frame of the request (a misread or invented timestamp) is moved to the nearest frame time."""
    notes = Notes()
    block: list[str] | None = None
    times = sorted(frame_times or [])
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _SHOWN_RE.match(line)
        if m:
            t = parse_ts(m.group(1))
            if t is not None:
                if times and not (times[0] - 2 <= t <= times[-1] + 2):
                    t = min(times, key=lambda x: abs(x - t))
                notes.shown.append((t, " ".join(m.group(2).split())))
            block = None
            continue
        for name, rx in _HEADINGS:
            hm = rx.match(line)
            if hm:
                block = getattr(notes, name)
                rest = _clean_item(hm.group(1))
                if rest:
                    block.append(rest)
                break
        else:
            if block is not None:
                item = _clean_item(line)
                if item:
                    block.append(item)
    notes.shown.sort(key=lambda x: x[0])
    return notes


# --------------------------------------------------------------------------- VIDEO sections
@dataclass(frozen=True)
class Event:
    t: float
    kind: str  # "SAID" | "SHOWN"
    text: str

    def line(self) -> str:
        return f"[{fmt_ts(self.t)}] {self.kind}: {self.text}"


def said_events(chunk: dict, text: str, segments: list[dict] | None) -> list[Event]:
    """Transcript of one audio chunk as SAID events: one per timed segment (absolute seconds) when the transcriber
    gave them, else the whole chunk at its start time."""
    out = []
    for s in segments or []:
        words = " ".join(str(s.get("text") or "").split())
        if words and isinstance(s.get("start"), (int, float)):
            out.append(Event(float(s["start"]), "SAID", words))
    if out:
        return out
    words = " ".join(text.split())
    return [Event(float(chunk.get("start") or 0.0), "SAID", words)] if words else []


def window_body(events: list[Event]) -> str:
    """Events in time order (SAID before SHOWN at the same second)."""
    ordered = sorted(events, key=lambda e: (round(e.t, 1), e.kind != "SAID"))
    return "\n".join(e.line() for e in ordered)


def split_window(start: float, end: float, events: list[Event],
                 limit: int = MAX_WINDOW_CHARS) -> list[tuple[float, float, str]]:
    """(start, end, body) parts of one window, each at most ~limit characters, cut between lines."""
    ordered = sorted(events, key=lambda e: (round(e.t, 1), e.kind != "SAID"))
    parts: list[list[Event]] = []
    cur: list[Event] = []
    size = 0
    for e in ordered:
        n = len(e.line()) + 1
        if cur and size + n > limit:
            parts.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        parts.append(cur)
    out = []
    for i, p in enumerate(parts):
        a = start if i == 0 else p[0].t
        b = end if i == len(parts) - 1 else parts[i + 1][0].t
        out.append((a, b, window_body(p)))
    return out


def assign(events: list[Event], bounds: list[tuple[float, float]]) -> list[list[Event]]:
    """Events per window: a window takes [its start, the next window's start); anything before the first window goes
    to the first, anything after the last to the last."""
    out: list[list[Event]] = [[] for _ in bounds]
    if not bounds:
        return out
    starts = [a for a, _b in bounds]
    for e in events:
        i = 0
        while i + 1 < len(starts) and e.t >= starts[i + 1]:
            i += 1
        out[i].append(e)
    return out
