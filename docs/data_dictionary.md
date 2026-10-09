# Data Dictionary — Quality Scorecard System (Cycle 1)

Per the Data-Driven Development Framework v1.1, §7.2: "For each field define data type,
length/format, business rules, nullability, keys/identity and validation rules." This
document is that contract, table by table, for every column created by
`backend/alembic/versions/0001_initial_schema.py` and mapped in `backend/app/models/`.

Conventions used below:
- **Data type** is the Postgres type as created by the migration.
- **Length/format** captures precision/scale, string length caps, or JSON shape where relevant.
- **Business rule** is the plain-English rule the column exists to support.
- **Nullability** is `NOT NULL` or `NULL` as enforced by the DB.
- **Keys/Identity** notes PK/FK/unique-constraint membership.
- **Validation rule** is the concrete DB-level mechanism (CHECK constraint, trigger, unique
  index) or, where validation is intentionally left to the application layer, says so
  explicitly.

All primary keys are client-generated UUIDv4 (`uuid.uuid4()` in Python), not
DB-sequence-generated. All tables except `scorecard_embeddings`, `chat_sessions`, and
`chat_messages` (and `audit_log`) carry `created_at`/`updated_at` timestamps
(`TIMESTAMPTZ`, `server_default = now()`, `updated_at` also `ON UPDATE now()`); those
three tables carry only `created_at` (`chat_sessions` also has `last_activity_at` in
place of `updated_at`) since they are append-mostly logs, not mutable records.

---

## 1. `users`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for a person who can own scorecards, evaluate, or design. | NOT NULL | Primary key | Generated client-side (`uuid.uuid4`). |
| `email` | VARCHAR | max 320 chars (RFC 5321 mailbox limit) | Sign-in identifier and the address for reset / verification mail. | NOT NULL | Unique (`uq_users_email`), indexed | Application-layer format check (plain string, not `EmailStr`, because seed accounts use a reserved TLD); normalised to lower case. |
| `name` | VARCHAR | max 200 chars | Display name. | NOT NULL | — | Non-empty enforced by Pydantic (`str`, no blank-check at DB level — left to app layer). |
| `org_id` | UUID | v4 | Reserved for future multi-tenant/org scoping. No `organizations` table exists yet in Cycle 1 (full RBAC/org model is deferred per the plan), so this is a plain scalar, not a FK. | NULL | — | None at DB level (deliberately deferred; documented gap). |
| `role` | VARCHAR | max 50 chars, default `'user'` (migration 0013) | `admin` or `user` for people; `system` marks the internal marker row. Legacy `member` / `designer` / `evaluator` were normalised to `user`. | NOT NULL | — | Validated in the API (`admin\|user`); not a DB enum. See `docs/plan-sharing-rbac.md`. |
| `auth_provider_id` | VARCHAR | max 255 chars | External OIDC subject id, reserved; OAuth links live in `oauth_identities`. | NULL | Unique (`uq_users_auth_provider_id`) | Uniqueness only; format is provider-specific and unvalidated in Cycle 1. |
| `created_at` | TIMESTAMPTZ | — | Row creation time. | NOT NULL | — | `server_default = now()`. |
| `updated_at` | TIMESTAMPTZ | — | Last row modification time. | NOT NULL | — | `server_default = now()`, `ON UPDATE now()`. |

---

## 2. `scorecards`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for a scorecard "definition" (the versionless shell; actual KPI content lives in `scorecard_versions`). | NOT NULL | Primary key | — |
| `name` | VARCHAR | max 255 chars | Human-chosen scorecard name. | NOT NULL | — | Non-empty via Pydantic; no DB-level uniqueness (two scorecards may share a name — `owner_id` + `name` is not unique by design, since re-use/cloning is an explicit product flow). |
| `owner_id` | UUID | v4 | The user who created/owns this scorecard. | NOT NULL | FK → `users.id` (`ON DELETE RESTRICT`) | A user with owned scorecards cannot be deleted (FK RESTRICT) — prevents orphaning scorecards. |
| `domain` | VARCHAR | max 120 chars | Free-text business domain/category (e.g. "Customer Support", "Software Engineering"), used for filtering/similarity grouping. | NULL | Indexed | None — open vocabulary by design (Cycle 1 has no fixed domain taxonomy). |
| `purpose_statement` | TEXT | unbounded | The scorecard's stated purpose (per the Quality Scorecard Framework's "purpose" step); also the primary text embedded for similarity search. | NULL | — | None at DB level. |
| `scope` | TEXT | unbounded | What is/isn't covered by this scorecard. | NULL | — | None at DB level. |
| `target_score` | NUMERIC | precision 4, scale 2 (`0.00`–`99.99` representable) | The target weighted score used for the QTC (Quality/Time/Cost) gate and bullet-chart target tick. | NULL | — | `CHECK (target_score IS NULL OR (target_score >= 0 AND target_score <= 10))` (`ck_scorecards_target_score_range`) — scores are on a 0–10 scale. |
| `status` | ENUM `scorecard_status` | `draft` \| `published` \| `archived` | Lifecycle state (see plan's "lifecycle states" scenario category). | NOT NULL, default `draft` | — | Postgres native enum — only the three listed values are representable at all. |
| `current_version_id` | UUID | v4 | Points at the `scorecard_versions` row currently considered "live" for this scorecard. Circular with `scorecard_versions.scorecard_id`; the FK is added via `ALTER TABLE` after both tables exist (see migration §"Deferred circular FK"). | NULL | FK → `scorecard_versions.id` (`ON DELETE SET NULL`, `use_alter=True`) | If the current version is deleted, this reverts to NULL rather than blocking the delete. |
| `created_at` / `updated_at` | TIMESTAMPTZ | — | Standard audit timestamps. | NOT NULL | — | `server_default = now()` (+`ON UPDATE now()` for `updated_at`). |

---

## 3. `scorecard_versions`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one immutable-in-spirit version of a scorecard's KPI tree/guidelines. | NOT NULL | Primary key | — |
| `scorecard_id` | UUID | v4 | The parent scorecard this version belongs to. | NOT NULL | FK → `scorecards.id` (`ON DELETE CASCADE`), indexed | Deleting a scorecard cascades to delete all its versions (and, transitively, KPI nodes/guidelines). |
| `version_number` | INTEGER | ≥ 1 by convention | Monotonically increasing version counter per scorecard (v1, v2, ...). | NOT NULL | Unique with `scorecard_id` (`uq_scorecard_version_number`) | Uniqueness enforced; the "increasing" property itself is an app-layer convention (not DB-checked, since e.g. gaps from a deleted draft version are acceptable). |
| `guideline_notes` | TEXT | unbounded | Free-text changelog/rationale for this version (e.g. "reworded guideline language for clarity"). | NULL | — | None. |
| `created_by` | UUID | v4 | The user who authored this version. | NOT NULL | FK → `users.id` (`ON DELETE RESTRICT`) | Prevents deleting a user who authored a version still on record. |
| `is_active` | BOOLEAN | — | Marks whether this version is the historically "active" one vs. superseded (distinct from `scorecards.current_version_id`, which is the pointer; `is_active` is a per-version flag useful when browsing version history). | NOT NULL, default `true` | — | None at DB level (app sets exactly one active version per scorecard by convention; not DB-enforced in Cycle 1 — documented gap, candidate for a Cycle 2/3 constraint). |
| `created_at` / `updated_at` | TIMESTAMPTZ | — | Standard audit timestamps. | NOT NULL | — | `server_default = now()` (+`ON UPDATE now()`). |

---

## 4. `kpi_nodes`

Self-referential materialized-path hierarchy (max depth 4), per the plan's `ltree`-based
design.

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one KPI/parameter node. | NOT NULL | Primary key | — |
| `scorecard_version_id` | UUID | v4 | Which scorecard version this node belongs to. | NOT NULL | FK → `scorecard_versions.id` (`ON DELETE CASCADE`), indexed | Deleting a version cascades to delete its whole KPI tree. |
| `parent_id` | UUID | v4 | Self-reference to the parent node; `NULL` for a Level-1 (root) node. | NULL | FK → `kpi_nodes.id` (`ON DELETE CASCADE`), indexed | Deleting a parent cascades to delete its subtree. |
| `path` | `LTREE` (via `sqlalchemy_utils.LtreeType`) | dot-separated labels, one label per ancestor (e.g. `a1b2c3....d4e5f6...`); each label is the owning node's UUID hex (32 lowercase hex chars — the only characters `ltree` labels reliably accept) | Materialized path for O(1) ancestor queries and fast subtree lookups. | NOT NULL | GiST index (`ix_kpi_nodes_path_gist`, `USING GIST (path)`) | Computed server-side by the API/seed layer as `parent.path + '.' + this_node_id.hex` (or just the label, for roots) — not user-supplied. No DB constraint ties `path` to `parent_id`/`level` consistency in Cycle 1 (documented gap — a mismatch would only arise from a bug in the write path, not from user input, since it is never client-settable via the API). |
| `level` | INTEGER | 1–4 | Depth in the hierarchy (Level1→Level4 per the Quality Scorecard Framework). | NOT NULL | — | `CHECK (level >= 1 AND level <= 4)` (`ck_kpi_nodes_level_range`). The API additionally refuses to create a child under a Level-4 parent (`422`), since level 5 would violate this CHECK. |
| `name` | VARCHAR | max 255 chars | The KPI/parameter's display name. | NOT NULL | Unique among siblings (see below) | `CREATE UNIQUE INDEX uq_kpi_nodes_sibling_name ON kpi_nodes (scorecard_version_id, COALESCE(parent_id, '00000000-...'::uuid), name)` — two siblings (same parent, or both roots of the same version) cannot share a name. Postgres treats `NULL` as always-distinct in a plain UNIQUE constraint, so root nodes (`parent_id IS NULL`) are normalized via `COALESCE` to a sentinel UUID so duplicate root names are still caught. |
| `weight` | NUMERIC | precision 5, scale 2 (`0.00`–`999.99` representable; business range is `0`–`100`) | This node's weight within its sibling group, as a percentage. | NOT NULL | — | `CHECK (weight >= 0 AND weight <= 100)` (`ck_kpi_nodes_weight_range`) **plus** the deferred constraint trigger `trg_kpi_node_weight_sum` (see below): all siblings' weights (same `parent_id`, or same `scorecard_version_id` for roots) must sum to exactly 100.00 (±0.01 tolerance) by the end of the transaction. A sibling group can legitimately be empty mid-transaction (e.g. after deleting the last sibling) — that is not checked. |
| `display_order` | INTEGER | — | UI ordering hint among siblings. | NOT NULL, default `0` | — | None — any integer, including duplicates/negatives, is accepted (purely presentational). |
| `created_at` / `updated_at` | TIMESTAMPTZ | — | Standard audit timestamps. | NOT NULL | — | `server_default = now()` (+`ON UPDATE now()`). |

**Weight-sum trigger detail** (`trg_kpi_node_weight_sum`, function `check_kpi_node_weight_sum()`):
a `DEFERRABLE INITIALLY DEFERRED` `CONSTRAINT TRIGGER` (`AFTER INSERT OR UPDATE OF weight,
parent_id, scorecard_version_id OR DELETE`, `FOR EACH ROW`) that only evaluates at
transaction `COMMIT` (or on `SET CONSTRAINTS trg_kpi_node_weight_sum IMMEDIATE`), so a
multi-row insert of a full sibling group is checked once, as a whole, rather than
rejecting each row before its siblings exist. A plain `CHECK` constraint cannot express
this because Postgres `CHECK` constraints cannot aggregate across other rows.

---

## 5. `kpi_guidelines`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one guideline rung. | NOT NULL | Primary key | — |
| `kpi_node_id` | UUID | v4 | The KPI this guideline rung belongs to. | NOT NULL | FK → `kpi_nodes.id` (`ON DELETE CASCADE`), indexed | Deleting a KPI node cascades to delete its guidelines. |
| `score_level` | INTEGER | 0–10 | Which of the 11 qualitative/quantitative rungs this row defines (per the Quality Scorecard Framework's 11-level 0–10 guideline model). | NOT NULL | Unique with `kpi_node_id` (`uq_kpi_guidelines_node_level`) | `CHECK (score_level >= 0 AND score_level <= 10)` (`ck_kpi_guidelines_score_level_range`). A KPI node is **not** required to have all 11 levels present (see the "missing guideline level" flawed-data scenario in the seed script) — completeness is an app-layer/Cycle-3 concern, not a DB constraint, since a node under active drafting legitimately has an incomplete set. |
| `qualitative_text` | TEXT | unbounded | The written description of what this score level means. | NOT NULL | — | Non-empty enforced by Pydantic; no DB-level length/emptiness check. |
| `quantitative_criteria` | JSONB | free-form object; two documented shapes in use: `{"metric": str, "operator": str, "value": number}` (numeric) or `{"category": str}` (categorical) | Machine-checkable criteria backing the qualitative text, where applicable (per the plan's "numeric vs categorical quantitative guidelines" business-variant scenario). | NULL | — | No JSON-schema enforcement at the DB level (Postgres `JSONB` accepts any valid JSON); shape is a documented app-layer convention only. |
| `created_at` / `updated_at` | TIMESTAMPTZ | — | Standard audit timestamps. | NOT NULL | — | `server_default = now()` (+`ON UPDATE now()`). |

---

## 6. `scorecard_embeddings`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one embedding row. | NOT NULL | Primary key | — |
| `scorecard_version_id` | UUID | v4 | The scorecard version this embedding represents (one embedding per version — re-embedding is an UPDATE, not a new row; see the "embedding-model change requiring backfill" migration scenario). | NOT NULL | FK → `scorecard_versions.id` (`ON DELETE CASCADE`), **unique** (`uq_scorecard_embeddings_version_id`) | Uniqueness enforced at the DB level — at most one embedding per version, always. |
| `embedding` | `VECTOR(1024)` (pgvector, via `pgvector.sqlalchemy.Vector`) | fixed 1024 dimensions (Amazon Titan Text Embeddings V2 output size, per the plan) | The similarity-search vector for this version's purpose/KPI text. | NOT NULL | HNSW index (`ix_scorecard_embeddings_embedding_hnsw`, `USING hnsw (embedding vector_cosine_ops)`) | pgvector enforces the fixed dimension (1024) at the type level — a vector of any other length is rejected on insert. Cycle 1 populates this with a deterministic synthetic vector (no live Bedrock/Titan call — that integration is Cycle 1c); values are real 1024-dim unit-scaled floats, just not produced by the real embedding model yet. |
| `embedding_model` | VARCHAR | max 100 chars | Which embedding model produced this vector (e.g. `amazon.titan-embed-text-v2:0`), so a model change can be detected and backfilled. | NOT NULL | — | None beyond NOT NULL — free text. |
| `source_text_hash` | VARCHAR | max 64 chars (SHA-256 hex digest length) | Hash of the source text that was embedded, so the caller can detect "does this embedding need to be refreshed" without re-hashing large text. | NOT NULL | — | None beyond NOT NULL — the hash algorithm/format is an app-layer convention (SHA-256 hex, in the seed script). |
| `created_at` / `updated_at` | TIMESTAMPTZ | — | Standard audit timestamps. | NOT NULL | — | `server_default = now()` (+`ON UPDATE now()`). |

---

## 7. `evaluations`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one evaluation run. | NOT NULL | Primary key | — |
| `scorecard_version_id` | UUID | v4 | Which exact scorecard version this evaluation was scored against (pins the guideline set used, even if the scorecard is later versioned again). | NOT NULL | FK → `scorecard_versions.id` (`ON DELETE RESTRICT`), indexed | A scorecard version with evaluations against it cannot be deleted — preserves evaluation history (see "version bump preserves old evaluations" migration scenario). |
| `name` | VARCHAR | max 255 chars | Human-readable label for this evaluation run. | NOT NULL | — | Non-empty via Pydantic. |
| `evaluated_by` | UUID | v4 | The user who ran/submitted this evaluation. | NOT NULL | FK → `users.id` (`ON DELETE RESTRICT`) | Prevents deleting a user with evaluation history on record. |
| `input_reference` | JSONB | free-form object (e.g. `{"ticket_id": ..., "source": ...}`) | Pointer/metadata describing what was evaluated (document id, source system, etc.) — the actual evaluated content is out of DB scope in Cycle 1. | NULL | — | None — free-form by design. |
| `status` | ENUM `evaluation_status` | `pending` \| `in_progress` \| `completed` \| `failed` | Evaluation lifecycle state (see "partial-draft-evaluation" lifecycle scenario). | NOT NULL, default `pending` | — | Postgres native enum. |
| `final_weighted_score` | NUMERIC | precision 5, scale 2 | The weighted-average final score (0–10) once scoring is complete. | NULL (until scored) | — | `CHECK (final_weighted_score IS NULL OR (final_weighted_score >= 0 AND final_weighted_score <= 10))` (`ck_evaluations_final_score_range`). |
| `rag_band` | ENUM `rag_band` | `band_10_9` \| `band_8` \| `band_7` \| `band_6` \| `band_5` \| `band_4` \| `band_3_0` | The 7-band RAG colour result the final score maps to (per the Quality Scorecard Framework's RAG palette — kept as a separate concept from the app's `#FFF700` brand palette). | NULL (until scored) | — | Postgres native enum; the score→band mapping itself (`rag_band_for_score()` in `app/models/enums.py`) is applied in application code, not the DB. |
| `submitted_at` | TIMESTAMPTZ | — | When the evaluation was finalized/submitted. | NULL (until submitted) | — | None beyond nullability. |
| `domain` | VARCHAR | max 120 chars | Denormalized copy of `scorecards.domain` (via `scorecard_version → scorecard`), for fast filtering/reporting without a join. | NULL | Indexed | None — kept in sync by the write path, not a DB trigger (documented as an intentional denormalization per the plan's schema sketch, not auto-synced at the DB level in Cycle 1). |
| `created_at` / `updated_at` | TIMESTAMPTZ | — | Standard audit timestamps. | NOT NULL | — | `server_default = now()` (+`ON UPDATE now()`). |

---

## 8. `evaluation_kpi_results`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one per-KPI scoring result. | NOT NULL | Primary key | — |
| `evaluation_id` | UUID | v4 | The evaluation this result belongs to. | NOT NULL | FK → `evaluations.id` (`ON DELETE CASCADE`), indexed | Deleting an evaluation cascades to delete its KPI results. |
| `kpi_node_id` | UUID | v4 | Which KPI node this result scores. | NOT NULL | FK → `kpi_nodes.id` (`ON DELETE RESTRICT`), indexed | A KPI node referenced by a result cannot be deleted. **Plus**: `trg_eval_kpi_result_version_match` (see below) — this node must belong to the same `scorecard_version_id` as the parent evaluation. |
| `score` | NUMERIC | precision 4, scale 2 | The judge's (or human override's) score for this KPI, 0–10. | NOT NULL | — | `CHECK (score >= 0 AND score <= 10)` (`ck_eval_kpi_results_score_range`). |
| `matched_guideline_level` | INTEGER | 0–10 | Which guideline rung (`kpi_guidelines.score_level`) the reasoning matched to — the "basis for the rating". | NULL | — | `CHECK (matched_guideline_level IS NULL OR (matched_guideline_level >= 0 AND matched_guideline_level <= 10))` (`ck_eval_kpi_results_matched_level_range`). Not enforced to equal a *row that actually exists* in `kpi_guidelines` for this node (documented gap — acceptable in Cycle 1 since guideline sets may be incomplete, per `kpi_guidelines` above). |
| `reasoning_text` | TEXT | unbounded | Written justification for the score (the "reasoning-before-score" requirement from the plan's judge design). | NULL | — | None at DB level. |
| `evidence_quotes` | JSONB | array or object of quoted evidence strings | Direct quotes from the evaluated input supporting the score (for the contextual-grounding check mentioned in the plan). | NULL | — | None — free-form. |
| `created_at` / `updated_at` | TIMESTAMPTZ | — | Standard audit timestamps. | NOT NULL | — | `server_default = now()` (+`ON UPDATE now()`). |

**Cross-version trigger detail** (`trg_eval_kpi_result_version_match`, function
`check_eval_kpi_result_version_match()`): a `BEFORE INSERT OR UPDATE OF evaluation_id,
kpi_node_id` `FOR EACH ROW` trigger (not deferred — fires immediately) that looks up
`evaluations.scorecard_version_id` for `NEW.evaluation_id` and `kpi_nodes.scorecard_version_id`
for `NEW.kpi_node_id`, and raises if they differ. This is the "cross-version KPI reference"
invalid-data scenario from the plan's catalogue — there is no plain FK for it because it
spans two tables through a third column, so it needed a trigger rather than a `CHECK`/FK.

---

## 9. `chat_sessions`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one chat-builder session. | NOT NULL | Primary key | — |
| `user_id` | UUID | v4 | The user driving this chat session. | NOT NULL | FK → `users.id` (`ON DELETE CASCADE`), indexed | Deleting a user deletes their chat sessions. |
| `status` | ENUM `chat_session_status` | `active` \| `completed` \| `abandoned` | Session lifecycle state. | NOT NULL, default `active` | — | Postgres native enum. |
| `context_summary` | TEXT | unbounded | Rolling summary of the conversation (for context-window management by the Cycle 1c LangGraph builder). | NULL | — | None. |
| `target_scorecard_id` | UUID | v4 | If this session is refining an existing scorecard rather than building one from scratch. | NULL | FK → `scorecards.id` (`ON DELETE SET NULL`) | If the target scorecard is deleted, this reverts to NULL rather than blocking the delete. |
| `created_at` | TIMESTAMPTZ | — | Session start time. | NOT NULL | — | `server_default = now()`. |
| `last_activity_at` | TIMESTAMPTZ | — | Last message time (used for session listing/sort). | NOT NULL | — | `server_default = now()`, `ON UPDATE now()`. |

---

## 10. `chat_messages`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one chat message. | NOT NULL | Primary key | — |
| `session_id` | UUID | v4 | The session this message belongs to. | NOT NULL | FK → `chat_sessions.id` (`ON DELETE CASCADE`), indexed | Deleting a session deletes its messages. |
| `role` | ENUM `chat_message_role` | `user` \| `assistant` \| `system` \| `tool` | Who/what produced this message. | NOT NULL | — | Postgres native enum. |
| `content` | TEXT | unbounded | The message text. | NOT NULL | — | Non-empty enforced by Pydantic where applicable; no DB-level check (a tool-call-only assistant turn may have minimal content). |
| `tool_calls` | JSONB | free-form (e.g. `{"tool": "ask_clarification", "args": {...}}`) | Structured tool-call payload, per the plan's "every LLM turn emits exactly one structured tool call" design. | NULL | — | None — free-form; schema validated against the Pydantic `ScorecardDraft`/tool schemas at the Cycle 1c application layer, not the DB. |
| `created_at` | TIMESTAMPTZ | — | Message time. | NOT NULL | — | `server_default = now()`. |

---

## 11. `audit_log`

| Field | Data type | Length/format | Business rule | Nullability | Keys/Identity | Validation rule |
|---|---|---|---|---|---|---|
| `id` | UUID | v4 | Unique identifier for one audit entry. | NOT NULL | Primary key | — |
| `actor_id` | UUID | v4 | The user who performed the action (NULL for system-initiated actions). | NULL | FK → `users.id` (`ON DELETE SET NULL`), indexed | Deleting a user does not delete their audit trail — the actor reference is nulled instead, preserving the log. |
| `entity_type` | VARCHAR | max 100 chars | Which table/entity kind was affected (e.g. `"scorecard"`). | NOT NULL | Indexed | Free text — no DB-level enum, since new entity types should not require a migration to log. |
| `entity_id` | UUID | v4 | The affected row's id (not a DB-level FK, since it can point at any table). | NOT NULL | Indexed | None — polymorphic reference is an intentional, app-layer-only relationship. |
| `action` | ENUM `audit_action` | `create` \| `update` \| `delete` | What happened. | NOT NULL | — | Postgres native enum. |
| `diff` | JSONB | free-form object | Before/after (or relevant metadata) for the change, e.g. `{"cloned_from_scorecard_id": "..."}`. | NULL | — | None — free-form by design. |
| `created_at` | TIMESTAMPTZ | — | When the action occurred. | NOT NULL | — | `server_default = now()`. |

---

## Cross-cutting DB-level validation summary

| Mechanism | Where | What it enforces |
|---|---|---|
| `CHECK` constraints | `scorecards.target_score`, `kpi_nodes.level`, `kpi_nodes.weight`, `kpi_guidelines.score_level`, `evaluations.final_weighted_score`, `evaluation_kpi_results.score`, `evaluation_kpi_results.matched_guideline_level` | Simple single-row numeric range rules. |
| Native Postgres `ENUM` types | `scorecard_status`, `evaluation_status`, `rag_band`, `chat_session_status`, `chat_message_role`, `audit_action` | Closed vocabularies — an out-of-set value cannot be stored at all. |
| Unique constraints/indexes | `users.email`, `users.auth_provider_id`, `scorecard_versions(scorecard_id, version_number)`, `kpi_guidelines(kpi_node_id, score_level)`, `scorecard_embeddings.scorecard_version_id`, `kpi_nodes` sibling-name index | Identity/no-duplicates rules. |
| `trg_kpi_node_weight_sum` (deferred constraint trigger) | `kpi_nodes` | Sibling weights sum to 100. |
| `trg_eval_kpi_result_version_match` (BEFORE trigger) | `evaluation_kpi_results` | A result's KPI node belongs to the same scorecard version as its evaluation. |
| GiST index | `kpi_nodes.path` | Fast `ltree` ancestor/subtree queries. |
| HNSW index | `scorecard_embeddings.embedding` | Fast approximate cosine-similarity search. |

Everything in the table above is proven against a real Postgres instance by
`backend/app/scripts/generate_scenarios.py`'s "invalid/flawed data" scenarios (weight-sum
97%/103%, duplicate sibling names, cross-version KPI reference, out-of-range score) and by
`backend/tests/test_weight_trigger.py`.

## Tables and columns added after migration 0001

Summary only; the ORM models in `backend/app/models/` and the migrations in `backend/alembic/versions/` are the source of truth. Diagram: [architecture.md](architecture.md#6-data-model).

| Migration | Change |
|---|---|
| `0002`-`0009` | Judge ensemble fields and review flag on `evaluation_kpi_results`; chat turn marker, `chat_turn_events` (live trace), session title, turn error columns; scoring formula and KPI flags; category nodes carry no weight |
| `0010_ai_eval_pipeline` | `evaluation_status` gains `queued`, `ingesting`, `processing` (unused), `scoring`; `evaluations` gains queue / progress / subject columns (`stage`, `progress`, `error_code`, `sfn_execution_arn`, `queued_at`, `started_at`, `finished_at`, `attempt`, ...); tables `evaluation_batches`, `evaluation_sources`, `evaluation_events`; `evaluation_kpi_results.jev_raw` |
| `0011_scaling_leases` | `evaluations.lease_owner`, `lease_expires_at`, `heartbeat_at`, `cancel_requested_at`; the same lease / cancel fields on `chat_sessions` prefixed `turn_`, plus `turn_message`, `turn_first` (the in-progress marker becomes a queued job); table `idempotency_keys` (PK `user_id, scope, key`) |
| `0012_auth_ownership` | `users`: `password_hash`, `email_verified_at`, `is_active`, `failed_logins`, `locked_until`, `sessions_valid_after`; tables `refresh_tokens` (hashed, `family_id`, `used_at`, `revoked_at`), `password_reset_tokens` (reset and verify tokens), `oauth_identities`; `evaluations.owner_id` (the owning user, distinct from `lease_owner`); `audit_log.ip`, `user_agent`, `request_id` |
| `0013_sharing_rbac` | `users.username` (unique on `lower()`), `users.deleted_at`; roles normalised to `admin`/`user`; tables `scorecard_collaborators` (unique `scorecard_id, user_id`), `scorecard_invitations` (status `pending/accepted/declined/revoked`, partial unique on pending), `scorecard_activity` (append-only: an UPDATE trigger raises), `notifications` (unique per-user `dedupe_key`); list and job-slot indexes on `evaluations` |
| `0014_chat_shares` | table `chat_shares` (unique `session_id, recipient_id`; `shared_by`, `with_chart`, `saved_scorecard_id`) |
| `0015_chart_trash` | `scorecards.deleted_at`, `deleted_by`; partial indexes for the trash listing and the retention purge |
| `0016_eval_cancel_reason` | `evaluations.cancel_reason` (`cancelled_by_trash` marks jobs to resume on restore) |

Evaluation statuses are `pending`, `in_progress`, `completed`, `failed`, `queued`, `ingesting`, `processing`, `scoring`; a cancelled evaluation is `failed` with `error_code = 'cancelled'`.

## Documented gaps (intentionally out of scope for Cycle 1, per the plan)

- `users.role` vocabulary, `kpi_nodes.path`/`level` internal consistency, `evaluations.domain`
  denormalization sync, and `evaluation_kpi_results.matched_guideline_level` referencing an
  actual existing `kpi_guidelines` row are all enforced by the application/seed layer, not
  the database, in Cycle 1. Candidates for Cycle 2/3 hardening if evidence from real usage
  shows they need DB-level enforcement.
- Row-Level Security (RLS) and org-level multi-tenancy are still not implemented: access control is enforced in the application layer (`backend/app/authz.py`), and `users.org_id` is an unused scalar.
  Authentication (password + rotating refresh tokens, optional OAuth) and two-role RBAC were added later; see `docs/plan-sharing-rbac.md`.
