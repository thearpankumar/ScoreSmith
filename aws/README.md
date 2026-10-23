# AWS side of the AI evaluation pipeline

Serverless extraction for file / Google Drive submissions. The backend starts one Step Functions execution per
evaluation, the Lambdas turn the files into text, and the local LangGraph pipeline scores `corpus.json`. The binding
contract (names, S3 layout, JSON schemas, error codes) is `docs/ai-eval-contract.md`.

```
Ingest -> Map files (2) -+- pdf/docx/md/txt -> ExtractDoc -> Map images (3) -> AnalyzeImage
                         +- video           -> PlanAudio  -> Map chunks (3) -> TranscribeChunk -> MergeTranscript
         -> Assemble  => derived/{eid}/corpus.json + corpus.md + progress.json
any failure -> HandleFailure (status.json + progress.json stage "failed") -> Fail
```

## Layout

| Path | What |
| --- | --- |
| `bootstrap.ps1` / `bootstrap.sh` | Idempotent describe-or-create of everything below. `-WhatIf` / `--dry-run` only prints. |
| `deploy-lambdas.ps1` / `.sh` | Rebuild the image, push, sync the OpenRouter key to SSM, then for each of the six functions re-apply its environment and `update-function-code`. Nothing else. |
| `teardown.ps1` / `.sh` | Deletes everything (asks you to type the bucket name). `-WhatIf` / `--dry-run` supported. |
| `statemachine.asl.json` | State machine; `${IngestFnArn}` etc. are substituted by bootstrap. |
| `policies/` | IAM / S3 / ECR / Budget JSON templates (`${ACCOUNT_ID}`, `${REGION}`, `${BUCKET}`). |
| `lambdas/` | One container image: `Dockerfile`, `src/worker/*`, `tests/` (pytest, no AWS or network). |

## What bootstrap creates (all in us-east-1, profile `arpan-aws`)

- **S3** `qs-eval-<account>-us-east-1`: Block Public Access, BucketOwnerEnforced, SSE-S3, deny-non-TLS policy, CORS
  for `http://localhost:3000` (+ `--cors-origin`), lifecycle `uploads/ raw/ batches/` 14 d, `derived/` 90 d, abort
  incomplete multipart uploads after 1 d.
- **ECR** `qs-eval-worker` (scan on push, keep last 5 images) and the image build/push.
- **IAM** `qs-eval-lambda-role` (S3: read `uploads/*`, read/write `raw/*` `derived/*`; `bedrock:InvokeModel` on only the
  Voxtral small/mini, Kimi K2.5, Qwen3-VL foundation models, the Maverick `us.` inference profile and its underlying
  regional foundation models; `ssm:GetParameter` on `/qs-eval/openrouter-api-key` only; its own log groups),
  `qs-eval-sfn-role` (`lambda:InvokeFunction` on the six functions only), user `qs-backend-app` (S3 put/get/abort/list
  on `uploads/ batches/ derived/`, `states:StartExecution` on the one state machine, `Describe/StopExecution` on its
  executions; no Bedrock, no admin).
- **SSM SecureString** `/qs-eval/openrouter-api-key` (default `aws/ssm` KMS key), written from `OPENROUTER_API_KEY` in
  `infra/.env` when that is set and non-empty. The last `OPENROUTER_API_KEY=` line wins, as in docker compose. The value
  is passed to the CLI in a private temp file and is never printed. Without a key the parameter is left as it is. If no
  parameter exists yet, the script prints a note: evaluations that use OpenRouter fail at ingest until a key is stored.
- **6 Lambda functions** `qs-eval-ingest|extract-doc|plan-audio|transcribe-chunk|analyze-image|assemble` (container
  image, x86_64, 3008 MB, 900 s, no VPC, 10 GB `/tmp` for `ingest` and `plan-audio`), 14-day log groups, and reserved
  concurrency (3/6/6/12/12/3) only when the account has >= 100 + 42 unreserved. Environment, set on create and on
  every update: `LOG_LEVEL=INFO` and `OPENROUTER_SECRET_PARAM=/qs-eval/openrouter-api-key`. That is the parameter's
  name, never the key.
- **Step Functions** Standard state machine `qs-eval-pipeline`.
- **AWS Budget** `qs-eval-monthly`, $25/month, e-mail alerts at 80% actual and 100% forecast.
- **infra/.env** (git-ignored): `S3_BUCKET`, `SFN_STATE_MACHINE_ARN` always; `AWS_APP_ACCESS_KEY_ID` /
  `AWS_APP_SECRET_ACCESS_KEY` only when the user has no access key yet. The secret is captured from the CLI and never
  printed. If a key exists but `.env` lacks the secret, run with `-RotateKey` / `--rotate-key`.

## Running it

Prerequisites: AWS CLI v2 with profile `arpan-aws` and Docker (Linux images; Docker Desktop on Windows is fine). For
Bedrock runs, model access must already be granted for the five models. For OpenRouter runs, put `OPENROUTER_API_KEY`
in `infra/.env` before running bootstrap or deploy-lambdas. Re-run either script after changing the key.

```powershell
.\aws\bootstrap.ps1 -WhatIf        # review the plan, no AWS calls
.\aws\bootstrap.ps1                # create / update (first run builds + pushes the image; ~5-10 min)
.\aws\deploy-lambdas.ps1           # after changing code under aws/lambdas/src
.\aws\teardown.ps1                 # remove everything
```

```bash
./aws/bootstrap.sh --dry-run && ./aws/bootstrap.sh
./aws/deploy-lambdas.sh
./aws/teardown.sh
```

Lambda unit tests (local, fakes only: no AWS, no network, no real key):

```bash
uv venv aws/lambdas/.venv --python 3.12
uv pip install --python aws/lambdas/.venv/Scripts/python.exe -r aws/lambdas/requirements-dev.txt   # bin/python on Linux/macOS
cd aws/lambdas && .venv/Scripts/python -m pytest -q
```

Plain `pip install -r requirements-dev.txt && python -m pytest -q` inside `aws/lambdas` works too. The tests clear every
`OPENROUTER_*` variable first, so a key in your shell is never used.

## AI provider per run

The backend picks the provider (an admin setting) and always sends it at the top level of the execution input:
`"provider": "bedrock" | "openrouter"`. The state machine passes the whole input to `ingest`, which saves it to
`derived/{eid}/run_config.json`. The transcription and image steps only receive S3 keys, so they read that file
(`worker/run_config.py`; cached per container for 60 s, because a retried evaluation keeps its id and may switch
provider). One deployment serves both providers.

- Missing or unknown `provider`, or a missing `run_config.json` (older backends), means Bedrock, exactly as before.
- Fail fast: for an OpenRouter run, `ingest` reads the key from SSM before fetching any file. If the parameter is
  missing, empty or denied, the run fails at once with `internal: OpenRouter key not configured in AWS (SSM parameter
  /qs-eval/openrouter-api-key: ...)`. It then calls OpenRouter's free `GET /key`: a rejected key (401/403) or a used-up
  credit limit fails the run the same way (`OpenRouter rejected the API key ...` / `... has no credit left ...`). Network
  trouble during that check does not fail the run. Otherwise the per-image and per-chunk Catch states would quietly
  skip every item.
- Code: `media.py` runs the two steps and dispatches to `bedrock_ops.py` or `openrouter_ops.py`. The outputs are
  identical either way: chunk `c{n}.txt` / `c{n}.json` with `model`, `tokens_in`, `tokens_out`; `.analysis.md` with
  the `<!-- source: ... | model: ... -->` header; and the progress markers. Shared code lives in `prompts.py` and
  `images.py` (size budget, re-encode, shrink-and-retry).

| Step | Bedrock (role auth) | OpenRouter (key from SSM) |
| --- | --- | --- |
| Transcription (`transcribe-chunk`) | Amazon Transcribe (batch job on the chunk in S3, language identified automatically), then `mistral.voxtral-small-24b-2507`, then `mistral.voxtral-mini-3b-2507` | `mistralai/voxtral-small-24b-2507` through chat `input_audio` (mp3), then `mistralai/voxtral-mini-3b-2507` through `/audio/transcriptions`, because Mini is not offered on chat |
| Image analysis (`analyze-image`) | `moonshotai.kimi-k2.5`, then `qwen.qwen3-vl-235b-a22b`, then Llama 4 Maverick | `qwen/qwen3.7-flash`, then `qwen/qwen3-vl-235b-a22b-instruct` |

Bedrock transcription (`worker/bedrock_ops.py`): since Oct 2026 Bedrock's Voxtral (now in its Legacy period) rejects audio with "Model does not support audio modality" on every API, so Bedrock runs use Amazon Transcribe. One job per chunk, named `qs-eval-{evaluation}-{source}-c{n}`: a retried step finds its job by name and keeps waiting (up to 720 s per attempt) instead of paying for a second one; a FAILED job is deleted and restarted once. The transcript comes back through Transcribe's pre-signed URL, so no output bucket is granted. The chunk JSON records `model: amazon.transcribe` with `tokens_in`/`tokens_out` = 0. IAM: `transcribe:StartTranscriptionJob` (this action has no resource type, so `*`) and Get/Delete on `transcription-job/qs-eval-*`. About $0.024 per audio minute.

OpenRouter details (`worker/openrouter_ops.py`):

- **Endpoint and headers.** `POST {OPENROUTER_API_BASE or https://openrouter.ai/api/v1}/chat/completions`. Requests
  send `HTTP-Referer` (`OPENROUTER_HTTP_REFERER`, default `https://scoresmith.app`), `X-OpenRouter-Title: ScoreSmith`
  and `X-OpenRouter-App-Visibility: hidden`.
- **Request settings.** Transcription uses temperature 0 and `max_tokens` 8000. Images use temperature 0,
  `max_tokens` 4000 and `"reasoning": {"enabled": false}`. In the probe, qwen3.7-flash took 11.6 s with its default
  reasoning and 1.6 s without it, with the same OCR. Set `OPENROUTER_VISION_REASONING=on` to omit the reasoning setting.
- **Errors.**
  - 401, 402 and 403: the step stops at once with an `internal` PipelineError. There is no retry and no fallback to
    the next model. That image or chunk is then skipped by its Catch state, without a `.failed` marker, and the run
    goes on without it. A 402 with `openrouter_in_flight_budget` is retried, and a 403 for flagged content only skips
    that model.
  - 413, or a 400 that mentions size: the image is shrunk and sent again.
  - 408, 429, 5xx and connection errors: jittered retries that honour `Retry-After` (capped at 30 s), within about
    120 s per call. Then the step fails with `TransientError`, which the state machine retries.
  - Any other 4xx, for example 404 "No endpoints found that can handle the requested parameters": the next model is
    tried.
  - A 200 that carries an `error`, at the top level or on the choice, is handled like the matching status.
- **Logs.** Keys (`sk-or-...`, `Bearer ...` and the exact key) are scrubbed from every error message and log line the
  module writes.
- **The key.** It is read from SSM once per container. `OPENROUTER_API_KEY` in the environment overrides it, for local
  runs only. A 401 drops the cached copy.
- **Overrides.** `OPENROUTER_TRANSCRIBE_MODELS` and `OPENROUTER_VISION_MODELS` take comma lists. A transcription id
  that is in `STT_MODELS`, or that contains `transcribe` or `whisper`, is called on `/audio/transcriptions`. Every
  bootstrap and deploy-lambdas run replaces the whole function environment. To keep an override, add it to the
  `lambda-env.json` that those scripts write; one set by hand in the console is lost on the next deploy.

## Image analysis

Each extracted image is analysed once, with the same prompt for both providers (`worker/prompts.py`). The model answers
in Markdown with these sections:

- `## Text (OCR)`: all visible text, in reading order.
- `## Description`: what kind of image it is, its elements and values.
- `## What it shows`: what the image is about and what it represents or tries to explain. That covers the process,
  structure, comparison, trend or argument, what a reader should take away, and how it supports the document. It is
  strictly grounded in what is visible: the model must not invent details, and must say so when the purpose is unclear.
- `## Summary`: one sentence.

When the execution input carries the scorecard `context`, a short preamble comes before the prompt. It holds the
scorecard name, domain, purpose and scope, plus a "never describe anything that is not in the image" guard. The output
cap is 4000 tokens. Images that are too large are re-encoded under a body budget of about 2.7 MB, shrinking further if
the service still refuses. `scripts/analyze_images.py`, the local Bedrock tool, keeps a copy of the same prompt, and a
test checks that the two match.

## Behaviour worth knowing

- Only S3 keys travel between states (256 KB payload limit). Per-file failures (corrupt PDF, undecodable image,
  failed chunk) become warnings in `corpus.json`; the run fails only if no usable text remains (`no_content`) or the
  Drive/limits checks leave nothing (`drive_*`, `file_too_large`, `unsupported_type`).
- Progress: each Lambda writes small per-file keys under `derived/{eid}/progress/`; `progress.json` is rebuilt from
  them (throttled to every ~2 s) and finalised by `assemble`.
- File types: PDF, DOCX, Markdown/plain text (`.md`, `.txt`, split into page-sized sections) and video. A file with no
  extension (a Drive file named `report (final)`) is identified by its content. Archives, legacy `.doc`, slides and
  images are skipped with a warning.
- A video with no audio track (a silent screen recording) is `skipped`, not `failed`: there is nothing to transcribe and
  the run continues with the other files.
- A corpus with fewer than 30 words in total (for example a file that only holds a GitHub link) fails with `no_content`.
- Throttling: the account's Lambda concurrency quota is shared by every evaluation (10 on a new account). A throttled
  invocation is retried up to 25 times with jittered backoff capped at 30 s, and the Map states are kept small
  (2 files, 3 images, 3 audio chunks) so one evaluation cannot take every slot.
- Google Docs are fetched as DOCX; Sheets and Slides as PDF. Folder listings are capped by Google at 50 files (warning).
- Models: see [AI provider per run](#ai-provider-per-run). Bedrock auth is the Lambda role; OpenRouter's key comes
  from SSM.
- Scorecard context: the execution input may carry `context` (what the scorecard is about). `ingest` saves it as
  `derived/{eid}/context.json` and `analyze-image` puts it in front of its prompt, so images are described with that
  scorecard in mind. Without it (or with older backends) the prompt is neutral. Transcription never uses it: the
  transcript must stay word for word.
- Git Bash on Windows works too: `bootstrap.sh` keeps its temp files in `C:/...` form so `aws.exe` can read the
  `file://` arguments.

## Costs (rough, us-east-1)

Idle cost is about $0: Lambda, Step Functions Standard, S3 and Budgets are pay-per-use; ECR storage is a few cents.
Per evaluation the dominant costs are model tokens (Bedrock, or OpenRouter credits for OpenRouter runs: Voxtral per
audio minute, vision model per image) and Lambda
GB-seconds (3 GB x run time). A typical submission (one 20-page PDF with 15 images and a 10-minute video) is on the
order of cents to under a dollar; Step Functions adds about $0.025 per 1,000 state transitions (a few hundred per
evaluation). S3 is cents per month with the 14/90-day lifecycles. The $25/month budget alerts before this matters.

## Security notes

- Drive links: https only, hosts limited to `drive.google.com`, `docs.google.com`, `drive.usercontent.google.com`.
  Only the Drive id is taken from user input; every URL is rebuilt server-side. Each host (and each redirect hop) is
  resolved and refused if any address is private, loopback, link-local or reserved. Redirects are followed manually and
  may only land on the allowlist or `*.googleusercontent.com` (Google's content host).
- Limits from the execution input: `max_file_bytes` is enforced while streaming (and from `Content-Length`),
  `max_files` across all sources. Uploaded keys must start with `uploads/`; `analyze_image` only reads keys under the
  evaluation's own `derived/{eid}/docs/{source_id}/images/` prefix.
- Magic bytes: PDF `%PDF`, DOCX is a zip with `word/document.xml` (compression-ratio and total-size caps), video
  container signature plus an ffmpeg probe. Extraction caps: 600 pages, 150 analysed images, 400 MB of extracted
  images, bounded render size, Pillow decompression-bomb limit, 6 h / 80 chunks of audio.
- No secrets in Lambda environment variables. Bedrock access is by role and limited to the named model ARNs. The
  OpenRouter key is an SSM SecureString that the role can read (that one parameter only). It is fetched only for
  OpenRouter runs, cached per container, and scrubbed from logs and error messages.
- DNS rebinding between the resolve check and the connection is not fully closed (the targets are Google hosts and
  user input never chooses the host); the IAM role has no access to anything but the bucket prefixes and the models.
- `qs-backend-app` can neither read `raw/*` nor invoke Bedrock nor touch other S3 buckets. Its key is in `infra/.env`
  only; rotate with `-RotateKey`.

## How the app uses this stack

- `bootstrap` writes `S3_BUCKET`, `SFN_STATE_MACHINE_ARN` and the `qs-backend-app` key pair (as `AWS_APP_ACCESS_KEY_ID` / `AWS_APP_SECRET_ACCESS_KEY`) into the git-ignored `infra/.env`.
  Without them everything else works and only the file / Google Drive evaluation flow answers 503. The Bedrock credential (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, used as a bearer token) is a different credential.
- The **worker** processes (`ROLE=worker`) start the executions, poll them (adopting executions of a dead worker by ARN) and score; the API replicas only presign uploads and enqueue. Any number of
  workers can share the one state machine. See [../docs/architecture.md](../docs/architecture.md#2-evaluation-pipeline-and-lease-lifecycle).
- For a deployed site pass the real origin to the bucket CORS rules (`--cors-origin https://app.example.com`) so browser multipart uploads work; the web app's CSP already allows `https://*.amazonaws.com` for uploads.
- Contract (S3 layout, JSON schemas, error codes): [../docs/ai-eval-contract.md](../docs/ai-eval-contract.md).
