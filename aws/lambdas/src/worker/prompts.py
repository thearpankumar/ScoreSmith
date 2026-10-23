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


def image_prompt(context: object) -> str:
    """IMAGE_PROMPT, preceded by what the evaluation's scorecard is about (see scorecard_context) so the description
    brings out the details that matter for that kind of evaluation. No context: the neutral IMAGE_PROMPT."""
    ctx = scorecard_context.clean(context)
    if not ctx:
        return IMAGE_PROMPT
    head = "Context: the document is being evaluated"
    if ctx.get("scorecard"):
        head += f' with the scorecard "{ctx["scorecard"]}"'
    if ctx.get("domain"):
        head += f" (domain: {ctx['domain']})"
    lines = [head + "."]
    if ctx.get("purpose"):
        lines.append(f"Purpose of the scorecard: {ctx['purpose']}")
    if ctx.get("scope"):
        lines.append(f"Scope: {ctx['scope']}")
    lines.append("Use this context only to decide which visible details are worth describing. "
                 "Never describe anything that is not in the image.")
    return "\n".join(lines) + "\n\n" + IMAGE_PROMPT
