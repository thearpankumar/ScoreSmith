"""What a scorecard is about (name, domain, purpose, scope), handed to every AI step of an evaluation so a submission
is judged for what THIS scorecard measures rather than a generic idea of quality (a customer-support scorecard and a
hackathon scorecard read "clear communication" very differently).

It is written by the scorecard's owner, so it is reviewer context and travels outside the untrusted `<corpus_data>`.
It only ever informs: it never overrides the KPI guidelines or the 0-10 scale. The AWS image step receives the same
fields through the execution input (`context`, see docs/ai-eval-contract.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _clip(value: Any, cap: int) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:cap] or None


@dataclass(frozen=True)
class ScorecardContext:
    name: str
    domain: str | None = None
    purpose: str | None = None
    scope: str | None = None

    MAX_TEXT = 2000  # purpose / scope characters kept (every KPI call carries them)

    @classmethod
    def of(cls, scorecard: Any) -> ScorecardContext:
        return cls(
            name=_clip(scorecard.name, 255) or "Untitled scorecard",
            domain=_clip(scorecard.domain, 120),
            purpose=_clip(scorecard.purpose_statement, cls.MAX_TEXT),
            scope=_clip(scorecard.scope, cls.MAX_TEXT),
        )

    def payload(self) -> dict[str, str]:
        """The execution input's `context` for the AWS pipeline; empty fields are left out."""
        fields = {"scorecard": self.name, "domain": self.domain, "purpose": self.purpose, "scope": self.scope}
        return {k: v for k, v in fields.items() if v}

    def state(self) -> dict[str, str]:
        """The Jev scorer's `scorecard` state entry."""
        fields = {"name": self.name, "domain": self.domain, "purpose": self.purpose, "scope": self.scope}
        return {k: v for k, v in fields.items() if v}

    def prompt_block(self) -> str:
        """Appended to a system prompt after its rules."""
        head = f'SCORECARD CONTEXT: the submission is evaluated with the scorecard "{self.name}"'
        if self.domain:
            head += f" (domain: {self.domain})"
        lines = [head + "."]
        if self.purpose:
            lines.append(f"Purpose: {self.purpose}")
        if self.scope:
            lines.append(f"Scope: {self.scope}")
        lines.append(
            "Use it to understand what this scorecard measures and to read each KPI in that light; it never "
            "overrides the KPI guidelines, the 0-10 scale or the rules above."
        )
        return "\n".join(lines)
