# Cycle 2 & Cycle 3 Plan — Quality Scorecard System

Status: **planning only — nothing in this document has been implemented.** Per the Data-Driven Development Framework v1.1 (`References/Data_Driven_Development_Framework_v1.1.pdf`), Cycle 1 (Data Foundation) is complete for this app: schema, data dictionary, the ~29-scenario catalogue in `backend/app/scripts/generate_scenarios.py`, and a working first build (chat-driven scorecard builder, LLM judge, Charts UI). Cycle 2 (Test & Migration) and Cycle 3 (Business Behaviour, Specification & Testing) were explicitly deferred. This document scopes both concretely for *this* app — not generic framework restatement.

---

## Cycle 2: Test and Migration

Framework definition (§8): "deliberately stresses the application with flawed data and migration conditions" — test design, buggy/invalid datasets, migration strategy, migration execution, defect report.

### 2.1 What the existing Cycle 1 catalogue already covers (don't duplicate)

`generate_scenarios.py` already seeds, among others: invalid weight sums (97/103), duplicate KPI names, cross-version KPI references, out-of-range scores, a missing guideline level, a version-bump-preserves-evaluations case, an embedding-model backfill case, a flat-to-hierarchical import, and a bulk-spreadsheet-import stub. Cycle 2 should **extend**, not re-cover, these.

### 2.2 New deliberately-broken/migration scenarios for Cycle 2

| # | Scenario | Why it's not covered yet | What it stresses |
|---|---|---|---|
| 1 | **Concurrent-edit conflict on the same `kpi_nodes` sibling group** | Two evaluators/designers PATCH weights on the same scorecard version at once (e.g. via `PATCH /kpi-nodes/weights` bulk endpoint from two browser tabs). Today there's no optimistic-concurrency check (`updated_at`/version column) on `KpiNode` — a lost-update race is currently silent. | Whether the weight-sum-to-100 DEFERRED trigger catches the corrupted end state, or whether two "valid" individual writes can still land on an invalid combined state. |
| 2 | **Partial-failure mid-materialization** | `draft_materialize.py`'s `materialize_draft` writes a `Scorecard` + `ScorecardVersion` + N `KpiNode`s + guidelines across one DB transaction, then `chat.py`'s `_maybe_materialize` does a **separate** best-effort `upsert_scorecard_embedding` call that's allowed to fail independently (already documented as best-effort in the code). Inject a forced embedding failure (kill Bedrock mid-call) and inspect: is the scorecard left fully saved but permanently unsearchable via similarity, with no retry/backfill path surfaced to the user? | Exactly the kind of "partial-failure mid-materialization state" the task calls out — currently a real, silent gap, not hypothetical. |
| 3 | **Embedding-model version drift at scale** | The existing `scenario_embedding_model_backfill` covers a single stale-embedding row. Cycle 2 should generate **volume**: N=500+ `scorecard_embeddings` rows at a stale dimension/model, then run `find_similar_scorecards` and confirm it either filters them out cleanly or degrades gracefully (not a pgvector dimension-mismatch 500 error) at realistic query volume, not just N=1. |
| 4 | **Bulk import of a real messy spreadsheet** | `scenario_bulk_spreadsheet_import` is a stub today. Cycle 2 should source (or synthesize) an actual messy `.xlsx`/`.csv` export from a real-world scorecard-like source (merged cells, inconsistent weight formatting like `"60%"` vs `60`, blank rows, a trailing totals row, duplicate KPI names with different casing, non-ASCII characters) and define the real ingestion contract: does the app even have a bulk-import endpoint yet? (It does not — `kpi-nodes/bulk` requires already-clean JSON.) This scenario should drive **building** a real spreadsheet-import path as a Cycle-2-informed feature, with explicit rejection rules for each malformed pattern found. |
| 5 | **Referential race on scorecard deletion mid-evaluation** | `DELETE /scorecards/{id}` cascades through `scorecard_versions` → `kpi_nodes` → `evaluation_kpi_results`. What happens if a `run_evaluation` call is in-flight (mid `asyncio.gather` ensemble calls) against a version that gets deleted concurrently? Today nothing prevents this. |
| 6 | **Ensemble judge score-persistence race** | New in this pass (Part 1 of the accompanying implementation): the k=3 ensemble's `asyncio.gather` across leaves means up to `3 * leaf_count` concurrent Bedrock calls per evaluation. A migration/stress scenario should push leaf_count high (e.g. the existing `scenario_2_vs_40_kpi`'s 40-KPI case) and confirm Bedrock throttling (`ThrottlingException`) surfaces as a clean `BedrockUnavailableError` → evaluation `FAILED`, not a partial write of some KPI results and not others. |
| 7 | **Migration-style legacy data with a pre-ensemble schema shape** | Now that `evaluation_kpi_results` has `needs_review`/`score_variance`/`ensemble_raw_scores` (migration `0002`), synthesize "legacy" rows as they'd have looked pre-migration (`needs_review=false` server default, `score_variance`/`ensemble_raw_scores` both `NULL`) and confirm every read path (API responses, any future UI that renders `needs_review`) treats `NULL` as "not flagged" rather than erroring — this is exactly the kind of schema-evolution scenario the framework's "Realism includes bad data" principle calls for, and it's now real (not hypothetical) because of this task's own schema change. |

### 2.3 Migration strategy for schema changes going forward

The project already has the right foundation (Alembic, one migration per schema change, additive-only so far — see `0001_initial_schema.py` → `0002_ensemble_judge_review.py`). Cycle 2 should formalize this into a written policy:

1. **Every schema change ships as a new Alembic revision**, never a hand-edited model without a migration (already the practice; make it an explicit rule/CI check — a pre-commit or CI step that runs `alembic check` or diffs `alembic revision --autogenerate` output against the committed migration).
2. **Additive-first**: new nullable columns with server defaults (as `0002` did) are always safe to ship ahead of application code that uses them; destructive changes (drop column/table, tighten a constraint) require a documented two-step migration (ship code that stops using the old shape → verify in production → THEN drop it in a later migration), never a single-step breaking change.
3. **Backfill scripts are separate from schema migrations.** A migration adds the column; a follow-up idempotent script (like `app/scripts/seed.py`'s pattern) backfills historical rows where needed, so a slow backfill never blocks `alembic upgrade head` from completing.
4. **Every migration gets a Cycle-2-style dry run** against a copy of production-shaped data (once real data exists) before it ships, using the same "run migrations against representative flawed datasets and observe failures" principle from the framework — extending the sandbox-Aurora dry run already described in `docs/cloud_deployment_plan.md` §2.1.
5. **Rollback discipline**: every migration's `downgrade()` must actually work (already true for `0001`/`0002` — both have real, tested-by-symmetry `downgrade()` bodies) and this should be enforced by a CI step that runs `upgrade head` → `downgrade -1` → `upgrade head` again on a scratch DB.

### 2.4 Defect report template

A lightweight, consistent format so Cycle 2 findings roll up into Cycle 3's refined PRD (per framework §8.5) instead of getting lost in ad-hoc notes:

```markdown
## Defect: <short title>

- **Scenario / dataset**: <which Cycle-2 scenario in generate_scenarios.py or new catalogue entry produced this>
- **Category**: data integrity | migration | concurrency | partial-failure | performance-at-scale
- **Severity**: blocks-launch | should-fix-before-cycle-3 | tracked-for-later
- **Observed behavior**: <what actually happened — include the raw error/trace>
- **Expected behavior**: <what should have happened per the current PRD/spec, or "spec silent — needs Cycle 3 decision">
- **Affected components**: <files/tables/endpoints>
- **Repro steps**: <exact steps or scenario-generation command>
- **Root cause** (once diagnosed): <e.g. missing optimistic lock, missing transaction boundary, missing null-check>
- **Fix**: <migration revision id and/or code change, PR link>
- **Regression test added**: <test file + test name that now covers this>
```

Each defect found in §2.2 above should produce one of these before Cycle 3 workflow modelling starts, since several of them (partial-failure materialization, concurrent-edit races) directly inform what Cycle 3's exception/alternate-flow modelling needs to formalize.

---

## Cycle 3: Business Behaviour, Specification and Testing

Framework definition (§9): complex workflows, business rules, state transitions, exceptions/alternate flows, refined PRD, acceptance suite, comprehensive automated suite.

### 3.1 Complex business workflows this app actually has but hasn't formalized

1. **Scorecard draft → published → archived lifecycle.** `ScorecardStatus` (`app/models/enums.py`) already defines `draft`/`published`/`archived`, but **no code enforces transitions** — `PATCH /scorecards/{id}` (`app/api/v1/scorecards.py`) lets any field, including `status`, be set to any value with zero validation (confirmed by reading the endpoint: it does a raw `setattr` loop over whatever fields are provided). Cycle 3 needs to define: who can publish (only the owner? any org member?), what must be true to publish (at least one leaf KPI? weights sum to 100 — already DB-enforced — but also: at least one guideline per leaf?), whether publishing should be blocked while an evaluation is `in_progress` against the *current* version, and what archiving does to in-flight evaluations against that version (nothing, today — archiving is just a label).
2. **The QTC gate rule (Quality Scorecard Framework §10.2) — not implemented at all.** The app currently computes and persists Quality only (`final_weighted_score`/`rag_band`). Time and Cost are not modeled anywhere in the schema — there's no `evaluations.due_at`/`time_met`, no `evaluations.cost_budget`/`cost_actual`. Per the framework, "green" is `Q AND T AND C`, not quality alone, so today's RAG band is really only the Q factor, mislabeled as the full gate result. Cycle 3 must decide: does this app's `evaluations` table grow `time_target`/`time_actual`/`cost_target`/`cost_actual` columns and a computed `qtc_result` (boolean, `Q_met AND T_met AND C_met`), or does QTC belong one level up at the ODTQRC/task level (a concept this app doesn't model at all yet — there's no "task" entity, only scorecards/evaluations)? This is a real scope decision, not just an implementation detail, and should be resolved before schema work starts.
3. **Red-diagnosis (framework §12) and Aptitude/Capability/Competency tracking (framework §13) — not implemented at all.** No `users` table field, enum, or endpoint anywhere touches aptitude/capability/competency levels (C1-C6), nor does anything classify a red result's cause (skill/aptitude/will). This is the single biggest unimplemented chunk of the Quality Scorecard Framework relative to what Cycle 1 built. Scoping it for Cycle 3 realistically means: (a) a `user_competencies` table (user_id, domain/work-type, level C1-C6, evidence_count, last_updated) separate from the existing `users` table so competency data doesn't bloat the core identity model; (b) a "diagnose this red" workflow triggered manually (not automatically — the framework is explicit that this is a judgement call, not an algorithm) from an evaluation's RAG-red result, prompting the reviewing lead to record skill/aptitude/will and a remedy; (c) explicitly **not** building the "3 reds = off the project" automation the framework marks as "proposed; subject to HR policy" — that stays a human process, not a Cycle 3 feature.
4. **State transitions and exceptions for the chat-builder session itself.** `ChatSessionStatus` (`active`/`completed`/`abandoned`) already exists, but nothing ever sets a session to `abandoned` — there's no timeout/cleanup job, and a user who navigates away mid-clarification leaves the session `active` forever. Cycle 3 should define the abandonment rule (e.g. no activity for N days → a scheduled job flips status) and what "resuming" an abandoned session should do (this task's Part 2 unsaved-changes-navigation-guard work is adjacent to, but not a substitute for, this).
5. **Multi-step evaluation approval workflow.** Today `run_evaluation` goes straight from `pending` → `in_progress` → `completed`/`failed` with no human-in-the-loop step, and there's no concept of an evaluation being reviewed/approved/disputed after the judge scores it — even though the new `needs_review` flag (this task's Part 1) is *designed* to prompt exactly that kind of human follow-up and currently has no workflow attached to it (it's just a flag sitting in the API response). Cycle 3 should define: who reviews a `needs_review=true` KPI result, what actions they can take (accept the median score / override it / re-run that KPI's ensemble), and whether an evaluation with any unresolved `needs_review` KPI can still count as "complete" for QTC purposes.

### 3.2 Business rules and state transitions to specify

- Scorecard: `draft → published` (preconditions: ≥1 leaf KPI, every leaf has ≥1 guideline, weights sum to 100 — already enforced at the KPI level but not re-checked at publish time), `published → archived` (postcondition: existing evaluations against archived versions remain readable but new evaluations against them should probably be blocked or warned).
- Evaluation: add an explicit `needs_review` resolution sub-state (`reviewed_accepted` / `reviewed_overridden` / unresolved) distinct from the existing `EvaluationStatus`, so "the judge finished" and "a human signed off on the flagged KPIs" aren't conflated.
- Chat session: `active → completed` (already happens on materialize) `active → abandoned` (new, needs a defined trigger).

### 3.3 Exceptions and alternate flows to capture

- Judge ensemble: what happens when fewer than k=3 calls succeed (e.g. 2 of 3 Bedrock calls throttle)? Today `asyncio.gather` with default settings propagates the first exception and the whole `run_judge` call fails the evaluation — worth deciding whether a degraded 2-of-3 (or even 1-of-3) result should be allowed with an automatic `needs_review=true`, rather than failing the whole evaluation over one throttled call.
- Materialization: the partial-failure case from Cycle 2 §2.2 item 2 needs an explicit alternate flow (retry embedding on next save? surface a "not yet searchable" indicator?).
- Chat builder: what happens if a user's message arrives for a session whose LangGraph checkpoint was manually purged (e.g. an ops cleanup script, or the exact kind of manual `DELETE FROM checkpoints` cleanup performed during this task's own live verification)? Today `get_session_state` returning `None` produces a 404 — confirm that's the intended behavior for a resumed-but-purged session, not a confusing dead end.

### 3.4 Refined PRD

Per framework §9.4, the refined PRD should consolidate: the existing `docs/initial_product_scope.md`, the data dictionary, the Cycle-1 scenario catalogue, the Cycle-2 defect findings (§2 above), and the workflow/state-machine decisions from §3.1-3.3 above — once those decisions are actually made (this document deliberately stops short of making them; it scopes the decisions that need to happen, per the task's explicit instruction not to rush-implement Cycle 3).

### 3.5 Acceptance test suite (business-readable, stakeholder-signable)

Scoped to the workflows in §3.1, one feature-level acceptance case per row (each backed by several automated internal tests per framework §9.5):

1. A scorecard cannot be published unless every leaf KPI has at least one guideline.
2. Archiving a scorecard version does not delete or corrupt its historical evaluations.
3. An evaluation KPI result flagged `needs_review` is visibly distinguishable in the evaluation detail view and cannot be silently ignored (exact UI mechanism TBD in the actual Cycle 3 build, not this planning pass).
4. A red (`rag_band` in the bottom two bands) evaluation prompts an explicit diagnose-the-red action available to the reviewing user.
5. An abandoned chat session (per the new abandonment rule) is excluded from "continue where you left off" but still viewable in full history.

### 3.6 Comprehensive automated test suite — coverage areas

Building on the existing 69-test backend suite (`backend/tests/`) and the frontend's `tsc`/`lint`/`build` gates:

- **Functional**: one test per business rule in §3.2 (state-transition validity, illegal-transition rejection).
- **Integration**: the full chat → materialize → evaluate → review workflow end-to-end (this task's Part 1/live-verification work is a manual instance of exactly this; Cycle 3 should turn it into an automated integration test using `FakeBedrockClient` for the scripted-disagreement case already proven in `tests/test_judge.py`, plus a separate real-credentials smoke test gated behind an env flag for CI environments that do have Bedrock access).
- **Data/migration**: every Cycle-2 scenario (§2.2) becomes a permanent regression test, not a one-off manual run.
- **Concurrency**: scenario 1 and 5 from §2.2 (concurrent weight edits, delete-during-evaluation) as explicit async test cases using `asyncio.gather` against the test DB, per the pattern already established in `tests/test_judge.py`'s concurrent-call testing.
- **Non-functional**: judge ensemble latency/cost at the 40-KPI scale (scenario 6, §2.2) as a benchmark test with a tracked threshold, not just a manual timing observation.
- **Smoke/sanity**: the three flows this task's own live verification exercised manually (real Bedrock judge ensemble run, real chat scorecard creation, real chat refinement) should become an automated (real-credentials, CI-gated) smoke suite so this kind of live check doesn't have to be redone by hand every time.

---

## Sequencing recommendation

Cycle 2 should run **before** Cycle 3 workflow modelling starts, because at least two Cycle 2 findings (partial-failure materialization, the `needs_review` flag having no attached workflow) directly shape what Cycle 3's exception/state-transition specification needs to cover — doing them in the framework's stated order, not in parallel, avoids specifying workflows around bugs that Cycle 2 would otherwise have caught first.
