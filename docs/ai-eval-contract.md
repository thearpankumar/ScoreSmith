# AI evaluation pipeline: shared contract

Single source of truth for the three build areas (`aws/`, `backend/`, `frontend/`). Approved plan: `~/.claude/plans/typed-tumbling-kay.md`.

## Resource names
- Region `us-east-1`, profile `arpan-aws`, prefix `qs-eval`.
- Bucket `qs-eval-<account-id>-us-east-1`. State machine `qs-eval-pipeline`. Lambdas `qs-eval-ingest|extract-doc|plan-audio|transcribe-chunk|analyze-image|assemble`. ECR repo `qs-eval-worker`.
- IAM: role `qs-eval-lambda-role`, role `qs-eval-sfn-role`, user `qs-backend-app`.
- Backend env vars: `OR_S3_BUCKET`, `OR_SFN_STATE_MACHINE_ARN`, `OR_AWS_APP_ACCESS_KEY_ID`, `OR_AWS_APP_SECRET_ACCESS_KEY` (written by `aws/bootstrap` into git-ignored `infra/.env`; never printed), `AI_EVAL_MAX_CONCURRENT` (default 3, clamp 1-5), `BEDROCK_MASTER_MODEL_ID=us.meta.llama4-maverick-17b-instruct-v1:0`.

## S3 layout
- `uploads/{user_id}/{upload_group_id}/{uuid}.{ext}`: browser uploads (staging). Lifecycle 14 d.
- `raw/{evaluation_id}/{source_id}.{ext}`: files downloaded from Drive by the `ingest` Lambda. Lifecycle 14 d.
- `batches/{upload_group_id}/{uuid}.{xlsx|csv}`: batch sheets (uploaded with purpose `batch_sheet`). Lifecycle 14 d.
- `derived/{evaluation_id}/...`: everything below. Lifecycle 90 d.
  - `manifest.json`: `{"files":[{"source_id","original_name","kind":"pdf|docx|text|video|other","raw_key","size","status":"ok|skipped|failed","warnings":[str]}],"drive":[{"source_id","url","state":"ok|inaccessible|quota|empty","files_found":int,"warnings":[str]}]}`
  - `docs/{source_id}/content.md`, `docs/{source_id}/images/NNNN_*.jpg` and `NNNN_*.analysis.md` (same text format as `scripts/analyze_images.py`)
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
 "sources":[{"source_id":"uuid","kind":"upload|drive","s3_key":"uploads/...","original_name":"a.pdf","drive_url":"https://drive.google.com/..."}]}
```
Execution name = `evaluation_id` (+ `-aN` attempt suffix on retry). The execution succeeds only when `corpus.json` has been written; failure paths write `status.json` + `progress.json` stage `failed`.

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
