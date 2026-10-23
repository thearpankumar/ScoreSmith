# AI evaluation pipeline: shared contract

Single source of truth for the three build areas (`aws/`, `backend/`, `frontend/`). Approved plan: `~/.claude/plans/typed-tumbling-kay.md`.

## Resource names
- Region `us-east-1`, profile `arpan-aws`, prefix `qs-eval`.
- Bucket `qs-eval-<account-id>-us-east-1`. State machine `qs-eval-pipeline`. Lambdas `qs-eval-ingest|extract-doc|plan-audio|transcribe-chunk|analyze-image|assemble`. ECR repo `qs-eval-worker`.
- IAM: role `qs-eval-lambda-role`, role `qs-eval-sfn-role`, user `qs-backend-app`.
- Backend env vars: `S3_BUCKET`, `SFN_STATE_MACHINE_ARN`, `AWS_APP_ACCESS_KEY_ID`, `AWS_APP_SECRET_ACCESS_KEY` (written by `aws/bootstrap` into git-ignored `infra/.env`; never printed), `AI_EVAL_MAX_CONCURRENT` (default 3, clamp 1-5), `BEDROCK_MASTER_MODEL_ID=us.meta.llama4-maverick-17b-instruct-v1:0`.

## S3 layout
- `uploads/{user_id}/{upload_group_id}/{uuid}.{ext}`: browser uploads (staging). Lifecycle 14 d.
- `raw/{evaluation_id}/{source_id}.{ext}`: files downloaded from Drive by the `ingest` Lambda. Lifecycle 14 d.
- `batches/{upload_group_id}/{uuid}.{xlsx|csv}`: batch sheets (uploaded with purpose `batch_sheet`). Lifecycle 14 d.
- `derived/{evaluation_id}/...`: everything below. Lifecycle 90 d.
  - `context.json` (written by `ingest` when the input has `context`): `{"scorecard","domain","purpose","scope"}`, known string fields only, clipped. Read by `analyze-image`; missing or unreadable means "no context".
  - `run_config.json` (written by `ingest`): `{"provider":"bedrock|openrouter"}`, the input's `provider` normalised (missing or unknown = `bedrock`). Read by `transcribe-chunk` and `analyze-image` to pick the provider's models; a missing or unreadable file means `bedrock`.
  - `manifest.json`: `{"files":[{"source_id","original_name","kind":"pdf|docx|text|video|other","raw_key","size","status":"ok|skipped|failed","warnings":[str]}],"drive":[{"source_id","url","state":"ok|inaccessible|quota|empty","files_found":int,"warnings":[str]}]}`
  - `docs/{source_id}/content.md`, `docs/{source_id}/images/NNNN_*.jpg` and `NNNN_*.analysis.md` (same text format as `scripts/analyze_images.py`). An analysis has the sections `## Text (OCR)`, `## Description`, `## What it shows` (what the image is about, what it represents or is trying to explain, and how it supports the document) and `## Summary`, whichever provider wrote it.
  - `video/{source_id}/audio.mp3`, `chunks/cN.mp3`, `transcript.txt`, `transcript.json`
  - `corpus.json`: `{"evaluation_id","built_at","stats":{"docs":int,"videos":int,"images":int,"words":int},"warnings":[str],"sections":[{"id":"s001","source_id","source_name","kind":"doc|video|image","label":"DOC a.pdf p3 | VIDEO demo.mp4 00:00-05:00 | IMAGE a.pdf fig 4","text":str}]}` (sections in reading order; docs get one section per page, video one per transcript chunk, image one per analysis)
  - `corpus.md`: same, stitched with `## [label]` headings
  - `progress.json`: `{"stage":"ingest|extract|transcribe|analyze|assemble|done|failed","updated_at":iso,"message":str,"files":[{"source_id","name","state":"pending|running|done|skipped|failed","detail":str}],"counters":{"files_total":int,"files_done":int,"images_total":int,"images_done":int,"chunks_total":int,"chunks_done":int}}`
  - `status.json` (written on failure): `{"error_code","error_message"}`

## Error codes
`drive_invalid`, `drive_inaccessible`, `drive_quota`, `drive_empty`, `file_too_large`, `unsupported_type`, `extract_failed`, `no_content`, `timeout`, `cancelled`, `scoring_failed`, `internal`.

## Step Functions input
```json
{"evaluation_id":"uuid","bucket":"...","limits":{"max_file_bytes":2147483648,"max_files":20},
 "sources":[{"source_id":"uuid","kind":"upload|drive","s3_key":"uploads/...","original_name":"a.pdf","drive_url":"https://drive.google.com/..."}],
 "context":{"scorecard":"Customer Support Quality Scorecard","domain":"Customer Support","purpose":"...","scope":"..."},
 "provider":"bedrock"}
```
`provider` (`"bedrock"` or `"openrouter"`; the backend always sends it) is the AI provider an admin selected in Settings when the execution started. `ingest` stores it in `derived/{evaluation_id}/run_config.json`; the transcription and image-analysis steps then call that provider's models (Bedrock: Amazon Transcribe, then Voxtral, and Kimi K2.5 / Qwen3-VL / Maverick; OpenRouter: Voxtral via OpenRouter, qwen3.7-flash / Qwen3-VL, with the key read from SSM). An input without the key (an older backend) runs on Bedrock. With `openrouter`, `ingest` fails the run at once if the Lambda cannot read its OpenRouter key from SSM (error_code `internal`, message starting `OpenRouter key not configured in AWS (SSM parameter /qs-eval/openrouter-api-key: ...)`), and likewise when OpenRouter rejects the key or its credit limit is used up (`OpenRouter rejected the API key ...` / `The OpenRouter key has no credit left ...`), instead of letting every image and audio chunk be skipped. On an OpenRouter run the `.analysis.md` headers and the per-chunk JSON (`video/{source_id}/chunks/cN.json`) name OpenRouter model ids (e.g. `qwen/qwen3.7-flash`, `mistralai/voxtral-small-24b-2507`); a chunk transcribed by Voxtral Mini (OpenRouter's speech-to-text endpoint) reports `tokens_in`/`tokens_out` = 0. On a Bedrock run the chunk JSON names `amazon.transcribe` (Bedrock's Voxtral rejects audio since Oct 2026), also with `tokens_in`/`tokens_out` = 0. The backend records the same value in `evaluations.ai_provider` and scores with it (see "AI provider" below).

`context` (optional; empty fields omitted) says what the scorecard is about. `ingest` saves it as `context.json` so image descriptions are written with the scorecard in mind; the local scoring graph gives the same context to every scoring step (see `backend/app/pipeline/scorecard_context.py`). It never overrides the KPI guidelines or the 0-10 scale. Older Lambdas ignore the field, and a Lambda without it uses a neutral prompt.

Execution name = `evaluation_id` (+ `-aN` attempt suffix on retry). The execution succeeds only when `corpus.json` has been written; failure paths write `status.json` + `progress.json` stage `failed`.

## AI provider (added with the Settings provider switch; additive)
- The provider is resolved once per evaluation, when its execution starts, and stored in `evaluations.ai_provider` (null until then). Scoring uses that provider, or the currently selected one if the stored provider is no longer available, so an evaluation that was running when an admin switched providers finishes on the provider it started with. `POST /evaluations/{id}/retry` clears it and the new attempt uses whichever provider is selected when it starts.
- Backend models per provider: master (evidence, reasoning, batch column mapping) Llama 4 Maverick or `openai/gpt-5-mini`; judge-tier fallback GLM-4.7-Flash or `openai/gpt-5-mini`. The Jev scoring gate is the same for both.

## Evaluation status values
`queued`, `ingesting` (Drive fetch + extraction + transcription on AWS), `processing` (unused alias kept in the enum), `scoring` (local LangGraph), `completed`, `failed`, plus legacy `pending`, `in_progress`. `stage` (free text column) mirrors `progress.json.stage` or `scoring:<substep>`.

## REST API (all under `/api/v1`, JSON snake_case, current-user dependency)
- `POST /evaluations/ai/uploads` body `{"purpose":"submission|batch_sheet","files":[{"name","size","content_type"}]}` → `{"upload_group_id","part_size","files":[{"client_index","upload_id","s3_key","parts":[{"part_number","url"}]}]}`. 15 min presigned part URLs. Server enforces extensions (`pdf docx md markdown txt mp4 mov mkv webm m4v` for submission; `xlsx csv` for batch_sheet), `max_file_bytes` 2 GiB, 10 files.
- `POST /evaluations/ai/uploads/complete` body `{"files":[{"upload_id","s3_key","parts":[{"part_number","etag"}]}]}` → `{"files":[{"s3_key","size","ok","error"}]}` (HeadObject size + magic-byte check).
- `POST /evaluations/ai/uploads/abort` body `{"files":[{"upload_id","s3_key"}]}` → 204.
- `POST /evaluations/ai/batches/parse` body `{"s3_key"}` → `{"rows":[{"row_index","email","name","drive_url","timestamp","warnings":[str]}],"skipped":[{"row_index","reason"}],"columns":{"email":"Email Address","name":"Name","drive_url":"Google Drive URL","timestamp":"Timestamp"}}`. Rows are already de-duplicated by email (latest timestamp wins).
- `POST /evaluations/ai/jobs` body `{"scorecard_id","direction_prompt":str|null,"items":[{"name":str|null,"subject_email":str|null,"subject_name":str|null,"sources":[{"kind":"upload","s3_key","original_name","size"}|{"kind":"drive","drive_url"}]}]}` → 202 `{"batch_id":uuid|null,"evaluations":[EvaluationSummary]}`. One item = one evaluation (uploaded files of one submission go in the same item). More than one item creates a batch. Drive URLs are validated (host allowlist) → 422 with per-item detail.
- `GET /evaluations/{id}/progress` → `{"evaluation_id","status","stage","queue_position":int|null,"error_code","error_message","progress":<progress.json or null>,"sources":[{"id","kind","original_name","drive_url","size","status","warnings"}],"events":[{"id","created_at","event_type","message"}]}`
- `POST /evaluations/{id}/cancel` → 202; `POST /evaluations/{id}/retry` → 202 (only from failed).
- `GET /evaluations/ai/batches/{id}` → `{"id","scorecard_id","status","total","counts":{"queued","running","completed","failed"},"evaluations":[EvaluationSummary]}`
- `EvaluationSummary` = existing evaluation read fields (camel mapping is done in the frontend client) plus `status`, `stage`, `subject_name`, `subject_email`, `batch_id`, `error_code`, `queued_at`, `started_at`, `finished_at`.
- Existing `GET /evaluations` and `GET /evaluations/{id}` also return those added fields. A completed evaluation still returns `EvaluationReadWithResults`.

## Jev mapping (scoring)
Jev `score` takes at most 10 criteria and returns a 0-indexed fractional position. Criteria are guideline levels 1-10; score = position + 1. A same-call `noul` ("any relevant evidence for this KPI?") below 0.10 gives score 0. Store `{position, probabilities, confidence, noul}` in `jev_raw`.

## AWS implementation notes (added by `aws/`; additive, nothing above changed)
- **Manifest, Drive-expanded files**: one Drive source can expand to many files (a folder). Each file gets `source_id = "{drive_source_id}-{NN}"` (NN = 01, 02, ...) and an extra optional field `parent_source_id` (the input `source_id`). `drive[].source_id` stays the input `source_id`. Upload sources keep their input `source_id`. Everything keyed by `{source_id}` below (`docs/`, `video/`, progress) uses the per-file id. Backend UI should group by `parent_source_id` when present.
- **Google Sheets / Slides links** are exported as PDF (Docs as DOCX), so the pipeline only ever ingests pdf/docx/text/video.
- **Extra derived objects** (safe to ignore): `video/{source_id}/plan.json`, `video/{source_id}/chunks/cN.txt|json`, `docs/{source_id}/extract.json` (stats + warnings or error), `docs/{source_id}/images/NNNN_*.analysis.md`, `progress/**` (per-file progress keys that `progress.json` is merged from).
- **Merge step**: the per-video transcript merge (`transcript.txt`/`transcript.json`) is done by the `assemble` function in `mode: "merge_video"`; the failure handler is the same function in `mode: "fail"`. There are still exactly six Lambdas.
- **Error codes from the pipeline**: `drive_*`, `file_too_large`, `unsupported_type`, `no_content`, `timeout`, `internal`. A single bad file is not fatal (it becomes a warning); the run fails only when no usable content remains.
- **Redirects**: Drive downloads may be redirected to `*.googleusercontent.com`; those hops (https, public IP) are allowed in addition to the three allowlisted hosts.
- **Image limit**: at most 150 images per document are analysed (warning added to `corpus.json.warnings`).
- **Text documents**: `.md`, `.markdown` and `.txt` files have `kind: "text"` and are split into page-sized sections (`## Page N` in `content.md`), so they appear in `corpus.json` as `doc` sections. A text file must not contain NUL bytes.
- **Content sniffing**: a Drive file whose name has no extension is classified from its first bytes (PDF `%PDF`, a zip is treated as a DOCX candidate and validated, video container signatures).
- **File states in `progress.json`**: `pending | running | done | skipped | failed`. A video with no audio track is `skipped` (warning `no audio track`), not `failed`.
- **Minimum content**: a corpus with fewer than 30 words fails with `no_content`.
- **Source rows**: a Drive source is the roll-up of its expanded files (`{source_id}-NN`): `running` while any file runs, `done` when any file was processed, `failed` only when none was. The backend applies the same roll-up to `evaluation_sources`.
- **Concurrency**: Step Functions Map states run 2 files, then 3 images or 3 audio chunks at a time. Lambda throttling is retried up to 25 times (jittered, capped at 30 s).

## Addendum: ownership, slots and workers (added after the original contract; the REST shapes above are unchanged)
- Every endpoint requires a signed-in user (cookie session or `Authorization: Bearer`). Evaluations and batches are visible to the owner and accepted collaborators of their chart; ids of anyone else's data answer `404`. Route table: `docs/api-reference.md`.
- `POST /evaluations/ai/jobs` is limited to **one running job per user** (a batch counts as one): `409 {"code":"user_job_active","evaluation_id","batch_id","scorecard_id","message"}`. It also accepts an `Idempotency-Key` header: a retried request with the same key returns the stored result instead of queuing again.
- The dispatcher runs in `ROLE=worker` processes. A claim stamps a lease (`lease_owner`, `lease_expires_at`, heartbeat) and any worker may adopt a row whose lease expired; cancellation is the DB flag `cancel_requested_at`. A cancelled evaluation is `status=failed`, `error_code=cancelled` (`cancel_reason=cancelled_by_trash` when the chart was trashed; restoring the chart re-queues those rows).
- `uploads/{user_id}/...` keys are bound to the user they were issued to, and each user has a rolling 24 h upload byte quota (`UPLOAD_USER_DAILY_BYTES`).
- Lists for the UI use `GET /evaluations/page` (keyset) and `POST /evaluations/refresh`; bulk delete and export accept a server-side filter. See `docs/architecture.md` for the lifecycle diagram.

