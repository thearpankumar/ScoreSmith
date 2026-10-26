"""Prompts for the media steps, shared by both providers (Bedrock and OpenRouter).

scripts/analyze_images.py keeps a copy of IMAGE_PROMPT for local runs; change both together.
"""
from __future__ import annotations

from . import scorecard_context

# tested wording; longer prompts made the model translate
TRANSCRIBE_PROMPT = "Transcribe this audio verbatim. Output only the transcript."
IMAGE_PROMPT = """You are analysing one image taken from a document submitted for evaluation (a report, slide deck,
solution document or similar).

Reply in Markdown with exactly these sections:

## Text (OCR)
Transcribe ALL text visible in the image exactly as written, in reading order. Keep table rows on one line each,
separated by " | ". If there is no text, write "(none)".

## Description
Say what kind of image this is (screenshot, architecture diagram, flow chart, chart/graph, table, photo, logo, other).
Describe the key elements, how they connect or flow, and any numbers/values shown. Be factual and concise.

## What it shows
Explain what the image is about and what it represents or is trying to explain: the process, structure, comparison,
trend or argument it conveys, what a reader should take away from it, and how it supports the document it was taken
from. Base this strictly on what is visible in the image and do not invent details; if its purpose is unclear, say so.

## Summary
One sentence."""
# output cap for one image analysis (both providers); leaves room for the "What it shows" section
IMAGE_MAX_TOKENS = 4000


VIDEO_FRAMES_PROMPT = """You are analysing frames sampled from a video submitted for evaluation (for example a product
demo, a screen recording, a presentation or a recorded procedure). The frames come as contact sheets: each image is a
grid of frames read left to right, then top to bottom, and every frame has its timestamp [mm:ss] printed in its
top-left corner. The frames are samples (one at each scene change and at least one every few seconds), so the picture
may have changed between two frames.

Reply in plain text in exactly this format:

[mm:ss] SHOWN: <what this frame shows: the screen, page, slide or scene; the text that matters (titles, field names,
values, messages, errors); and what the person does or what changed since the previous frame>
(one line per frame worth describing, in time order, each with the timestamp printed on that frame; when several
frames show the same thing, describe it once at the first of them and say until when it stays)

Steps observed, in order:
1. [mm:ss] <an action or step the frames show being done>
(only steps the frames show; write "(none)" if no procedure or action is visible)

Visual quality: <how readable the frames are: text legibility, focus, lighting, framing, steadiness, anything that
hides content>

Not visible in sampled frames: <what this kind of video would be expected to show, or what the scorecard looks for,
that none of these frames shows; "(nothing notable)" if nothing>

Rules: describe only what is visible in the frames. Never guess what happened between two frames, never invent text
you cannot read (write "unreadable" instead), and never claim that something did not happen in the video, only that
it is not visible in these frames."""
# KPI names put in front of the frames prompt (clipped; context.json keeps up to 60)
VIDEO_PROMPT_MAX_KPIS = 40


def _context_head(ctx: dict, subject: str) -> list[str]:
    head = f"Context: the {subject} is being evaluated"
    if ctx.get("scorecard"):
        head += f' with the scorecard "{ctx["scorecard"]}"'
    if ctx.get("domain"):
        head += f" (domain: {ctx['domain']})"
    lines = [head + "."]
    if ctx.get("purpose"):
        lines.append(f"Purpose of the scorecard: {ctx['purpose']}")
    if ctx.get("scope"):
        lines.append(f"Scope: {ctx['scope']}")
    return lines


def video_frames_prompt(context: object, frame_list: str) -> str:
    """VIDEO_FRAMES_PROMPT for one request: the scorecard context and the names of the KPIs it grades (so the model
    looks for those steps), the request's frame list, then the instructions. No context: frame list + prompt."""
    ctx = scorecard_context.clean(context)
    lines: list[str] = []
    if ctx:
        lines = _context_head(ctx, "video")
        kpis = ctx.get("kpis") or []
        if kpis:
            lines.append("The scorecard grades: " + "; ".join(kpis[:VIDEO_PROMPT_MAX_KPIS]) + ".")
        lines.append("Use this context only to decide which visible details, actions and steps are worth describing. "
                     "Never describe anything that is not in the frames.")
        lines.append("")
    lines.append(frame_list)
    return "\n".join(lines) + "\n\n" + VIDEO_FRAMES_PROMPT


def image_prompt(context: object) -> str:
    """IMAGE_PROMPT, preceded by what the evaluation's scorecard is about (see scorecard_context) so the description
    brings out the details that matter for that kind of evaluation. No context: the neutral IMAGE_PROMPT."""
    ctx = scorecard_context.clean(context)
    if not ctx:
        return IMAGE_PROMPT
    lines = _context_head(ctx, "document")
    lines.append("Use this context only to decide which visible details are worth describing. "
                 "Never describe anything that is not in the image.")
    return "\n".join(lines) + "\n\n" + IMAGE_PROMPT
