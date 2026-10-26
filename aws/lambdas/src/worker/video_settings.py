"""Knobs of the video visual analysis (frame sampling, contact sheets, vision requests).

Defaults come from the Lambda environment (the names below); the backend may override any of them for one run with
the execution input's `video` object, whose keys are the same names in lower case (e.g. {"frame_max_gap_s": 30,
"grid": "2x2"}). ingest saves the cleaned overrides in run_config.json, so every video step of the run sees the same
values. Payload and environment values are untrusted: unknown keys are dropped, numbers are clamped to sane ranges and
an unparsable value falls back to the default.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace

MODES = ("frames", "off")
_GRID_RE = re.compile(r"^\s*([1-4])\s*[xX]\s*([1-4])\s*$")


@dataclass(frozen=True)
class VideoSettings:
    mode: str = "frames"            # VIDEO_VISUAL_MODE: "frames" (analyse sampled frames) or "off"
    min_gap_s: float = 2.0          # FRAME_MIN_GAP_S: frames are sampled this often (the densest sampling) ...
    max_gap_s: float = 20.0         # FRAME_MAX_GAP_S: ... and at least this often in a long video (~900 samples)
    change_threshold: float = 0.001  # FRAME_CHANGE_THRESHOLD: share of pixels that must change for a new picture
    max_frames: int = 108           # MAX_FRAMES_PER_VIDEO: 12 sheets of 3x3, 3 requests of 4 sheets
    cols: int = 3                   # GRID "COLSxROWS"; 2x2 gives larger cells for text-heavy screen recordings
    rows: int = 3
    sheets_per_request: int = 4     # SHEETS_PER_REQUEST
    max_output_tokens: int = 1500   # VISUAL_MAX_OUTPUT_TOKENS: answer cap of one vision request

    @property
    def grid(self) -> str:
        return f"{self.cols}x{self.rows}"

    @property
    def per_sheet(self) -> int:
        return self.cols * self.rows

    def summary(self) -> dict:
        """The values a run used (recorded in sheets.json)."""
        return {"video_visual_mode": self.mode, "frame_min_gap_s": self.min_gap_s, "frame_max_gap_s": self.max_gap_s,
                "frame_change_threshold": self.change_threshold,
                "max_frames_per_video": self.max_frames, "grid": self.grid,
                "sheets_per_request": self.sheets_per_request, "visual_max_output_tokens": self.max_output_tokens}


def _mode(v: object) -> str | None:
    m = v.strip().lower() if isinstance(v, str) else ""
    return m if m in MODES else None


def _grid(v: object) -> tuple[int, int] | None:
    m = _GRID_RE.match(v) if isinstance(v, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def _number(kind, lo: float, hi: float):
    def parse(v: object):
        if isinstance(v, bool):
            return None
        try:
            n = kind(float(v)) if isinstance(v, (int, float, str)) else None
        except (TypeError, ValueError, OverflowError):
            return None
        if n is None or n != n:  # NaN
            return None
        return min(hi, max(lo, n))
    return parse


# knob name (env var; lower case in the per-run overrides) -> (field, parser returning the value or None)
KNOBS = {
    "VIDEO_VISUAL_MODE": ("mode", _mode),
    "FRAME_MIN_GAP_S": ("min_gap_s", _number(float, 0.5, 120.0)),
    "FRAME_MAX_GAP_S": ("max_gap_s", _number(float, 1.0, 600.0)),
    "FRAME_CHANGE_THRESHOLD": ("change_threshold", _number(float, 0.00005, 0.5)),
    "MAX_FRAMES_PER_VIDEO": ("max_frames", _number(int, 1, 540)),
    "GRID": ("grid", _grid),
    "SHEETS_PER_REQUEST": ("sheets_per_request", _number(int, 1, 8)),
    "VISUAL_MAX_OUTPUT_TOKENS": ("max_output_tokens", _number(int, 256, 8000)),
}


def clean(raw: object) -> dict:
    """Valid per-run overrides only, keyed by the lower-case knob name and already parsed (grid stays "CxR")."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for name, (_field, parse) in KNOBS.items():
        key = name.lower()
        if key in raw:
            value = parse(raw[key])
            if value is None:
                print(f"    ignoring video setting {key}={str(raw[key])[:40]!r}")
                continue
            out[key] = f"{value[0]}x{value[1]}" if name == "GRID" else value
    return out


def _apply(base: VideoSettings, values: dict) -> VideoSettings:
    """values: knob name -> raw value; invalid ones are skipped."""
    changes: dict = {}
    for name, (field, parse) in KNOBS.items():
        if name not in values:
            continue
        value = parse(values[name])
        if value is None:
            continue
        if field == "grid":
            changes["cols"], changes["rows"] = value
        else:
            changes[field] = value
    s = replace(base, **changes)
    if s.max_gap_s < s.min_gap_s:
        s = replace(s, max_gap_s=s.min_gap_s)
    return s


def resolve(overrides: object = None, env=None) -> VideoSettings:
    """Environment defaults, then the run's overrides."""
    env = os.environ if env is None else env
    s = _apply(VideoSettings(), {k: env[k] for k in KNOBS if str(env.get(k, "")).strip()})
    return _apply(s, {k.upper(): v for k, v in clean(overrides).items()})


def load(store, eid: str) -> VideoSettings:
    """The run's settings. A missing run_config.json means environment defaults; an S3 hiccup is a TransientError
    (see run_config.load_doc), so a step never silently runs with the wrong settings."""
    from . import run_config

    return resolve(run_config.load_doc(store, eid).get("video"))
