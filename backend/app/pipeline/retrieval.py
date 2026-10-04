"""Deterministic section retrieval for the scoring pipeline.

The master model chooses which parts of a long submission to read for each KPI, and its choice is the weak
point (an audit found it citing an unrelated table while the real evidence sat on another page). A plain
keyword ranking over every section is a cheap second opinion that does not depend on the model: it is unioned
with the model's choice on the first pass and is the sole source for the "second look" at low-scoring KPIs.

BM25-style scoring, pure Python, no dependencies.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable

from app.models.kpi_guideline import KpiGuideline
from app.models.kpi_node import KpiNode
from app.pipeline.corpus import Section

_WORD = re.compile(r"[a-z0-9][a-z0-9\-]{2,}")
_STOP = frozenset(
    """the and for are but not you all can had her was one our out has have this that with from they been were
    will would there their what when which who how its into than then them these those such also each any more
    most other some only over very about after again against because before being below between both does doing
    down during further here itself just made make many may might must off once own same should so still too
    under until upon where while whom why your per via using used use""".split()
)

# Guideline levels at or above this describe what "meeting" the KPI looks like; the low levels mostly say
# "absent / missing", which would only add noise to a search query.
_QUERY_MIN_LEVEL = 3


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def kpi_query(kpi: KpiNode, guidelines: Iterable[KpiGuideline]) -> str:
    """The KPI's name plus what its higher guideline levels say: the vocabulary of 'the KPI is met'."""
    parts = [kpi.name]
    for g in sorted(guidelines, key=lambda g: g.score_level):
        if g.score_level >= _QUERY_MIN_LEVEL and g.qualitative_text:
            parts.append(g.qualitative_text)
    return "\n".join(parts)


def rank_sections(
    sections: list[Section], query: str, k: int, exclude: frozenset[str] = frozenset()
) -> list[Section]:
    """The `k` sections that best match `query` (score > 0), best first. `exclude` skips section ids."""
    terms = set(tokenize(query))
    if not terms or k <= 0:
        return []
    docs = [(s, Counter(tokenize(s.text))) for s in sections if s.id not in exclude]
    if not docs:
        return []
    n = len(docs)
    avg_len = (sum(sum(c.values()) for _, c in docs) / n) or 1.0
    df = {t: sum(1 for _, c in docs if t in c) for t in terms}
    scored: list[tuple[float, int, Section]] = []
    for order, (section, counts) in enumerate(docs):
        length = sum(counts.values()) or 1
        norm = 0.25 + 0.75 * (length / avg_len)
        score = 0.0
        for t in terms:
            tf = counts.get(t, 0)
            if tf:
                idf = math.log(1.0 + (n - df[t] + 0.5) / (df[t] + 0.5))
                score += idf * (tf * 2.2) / (tf + 1.2 * norm)
        if score > 0:
            scored.append((score, order, section))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [s for _, _, s in scored[:k]]
