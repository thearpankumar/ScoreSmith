# Initial Product Scope — Quality Scorecard System (Score Smith)

> Historical document: this is the scope agreed at the start of Cycle 1. Since then the product was renamed **Score Smith** and gained authentication, two roles (`admin` / `user`), chart and chat sharing, notifications, a chart trash, an AI evaluation pipeline for files and Drive links, and a worker-based scaling model. Current behaviour: [README](../README.md), [architecture](architecture.md), [sharing and RBAC](plan-sharing-rbac.md).

Per Data-Driven Development Framework v1.1 §6. Deliberately lighter than a PRD; the detailed PRD is refined after Cycles 1–3 produce evidence.

## Problem

Quality of work (documents, tasks, projects, deliverables of any kind) is currently judged inconsistently — either not reviewed at scale, self-declared, or rated by an LLM with no agreed rubric behind the number. There is no system for defining *what quality means* for a given type of work (KPIs, guidelines, weights, target) and then applying that definition consistently and repeatably to rate real inputs, while building a reusable library of such definitions over time.

## Actors

- **Scorecard designer** — a business user who defines a new scorecard (purpose, KPIs, guidelines, weights, target) via a chat-driven builder, or adapts an existing one.
- **Evaluator** — a user who applies an existing scorecard to a real input (document/text/data) and receives a rated, explained result.
- **Judge (LLM)** — scores each KPI against its guidelines and produces reasoning; not a human actor, but a first-class system role.
- **Org/owner (implicit in Cycle 1, full RBAC deferred)** — owns and filters saved scorecards and evaluations.

## Expected outcomes

- A scorecard can be created from a natural-language prompt through a clarifying-question chat flow, without losing progress, and saved under a user-chosen name.
- A new request that resembles a previously saved scorecard surfaces that scorecard as a reuse/adapt suggestion before generating from scratch.
- An evaluation against a saved scorecard produces a per-KPI score (0–10), written reasoning citing the matched guideline level, a weighted final score, and a RAG colour band — viewable and saved in a dedicated Charts/Evaluations UI.
- Scorecards support an arbitrary Level1→Level4 parameter hierarchy and per-KPI weights, not a fixed flat list.

## Constraints

- Single PostgreSQL database (pgvector + ltree extensions) for structured, semi-structured (JSONB), and vector-similarity data — no polyglot persistence.
- LLM backend must be AWS Bedrock (Z.ai GLM-5 via Converse/ConverseStream for chat and judge; Titan embeddings for similarity).
- Local development entirely via Docker Compose.
- Judge reliability matters more than judge speed — reasoning-before-score, evidence citation required.
- Cycle 1 intentionally defers: complex multi-step business workflows, deliberately-flawed/migration data hardening (Cycle 2), production auth/RLS (authentication and RBAC have since been built; RLS has not), and full automated test suites (Cycle 3) — see `plans/polished-swimming-sun.md` for the phased roadmap.

## Broad functional scope (Cycle 1)

1. Scorecard CRUD with versioning, hierarchical KPI tree, per-KPI 11-level qualitative + quantitative guidelines, weights.
2. Chat-driven scorecard builder (LangGraph state machine, clarifying questions, live draft preview) backed by Bedrock.
3. Embedding-based "suggest similar scorecard" on new requests.
4. LLM-judge evaluation flow producing per-KPI scored, reasoned results + weighted final score + RAG band.
5. Charts library/detail UI (Overview, Guidelines, Evaluate, History) and an Evaluations history view, in a glassmorphism design system (originally white + lemon `#FFF700`; now warm off-white + amber/gold `#FFC21A`, see `frontend/README.md`).
