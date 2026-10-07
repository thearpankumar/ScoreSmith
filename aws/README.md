# AWS side of the AI evaluation pipeline (OpenRouter variant)

Serverless extraction for file / Google Drive submissions. The backend starts one Step Functions execution per
evaluation, the Lambdas turn the files into text, and the local LangGraph pipeline scores `corpus.json`. The binding
contract (names, S3 layout, JSON schemas, error codes) is `docs/ai-eval-contract.md`.

This branch (`openrouter-integration`) replaces Amazon Bedrock with **OpenRouter** (plain HTTPS from the Lambdas) and
deploys as its **own `qs-or` stack**. Every resource name, the IAM user and the `infra/.env` variables differ from the
Bedrock deployment on the main branch, so the two can live side by side in one account and neither script can touch
the other's resources.

```
Ingest -> Map files (2) -+- pdf/docx/md/txt -> ExtractDoc -> Map images (3) -> AnalyzeImage
                         +- video           -> PlanAudio  -> Map chunks (3) -> TranscribeChunk -> MergeTranscript
         -> Assemble  => derived/{eid}/corpus.json + corpus.md + progress.json
any failure -> HandleFailure (status.json + progress.json stage "failed") -> Fail
```

## Layout

| Path | What |
| --- | --- |
| `config.ps1` / `config.sh` | The single source of names (prefix `qs-or`, region, SSM parameter, `.env` variable names, Lambda environment). Sourced by every script below so they cannot drift. |
| `bootstrap.ps1` / `bootstrap.sh` | Idempotent describe-or-create of everything below. `-WhatIf` / `--dry-run` only prints. |
| `deploy-lambdas.ps1` / `.sh` | Rebuild the image, push, `update-function-code` and re-apply the Lambda environment for the six functions. Nothing else. |
| `teardown.ps1` / `.sh` | Deletes everything of the `qs-or` stack (asks you to type the bucket name). `-WhatIf` / `--dry-run` supported. |
| `statemachine.asl.json` | State machine; `${IngestFnArn}` etc. are substituted by bootstrap. |
| `policies/` | IAM / S3 / ECR / Budget JSON templates. Placeholders (`${ACCOUNT_ID}`, `${REGION}`, `${BUCKET}`, `${PREFIX}`, `${APP_USER}`, `${STATE_MACHINE}`, `${BUDGET_NAME}`, `${SSM_PARAM}`, `${BUDGET_EMAIL}`) are rendered from `config.*`. |
| `lambdas/` | One container image: `Dockerfile`, `src/worker/*` (`openrouter_ops.py` talks to OpenRouter), `tests/` (pytest, no AWS or network). |

## Prerequisites

- AWS CLI v2 with the profile `arpan-aws` (the scripts always use `--profile arpan-aws --region us-east-1`; override the
  profile with `-AwsProfile NAME` / `--profile NAME`).
- Docker running (Linux images are built locally; Docker Desktop on Windows is fine).
- `OPENROUTER_API_KEY=...` in `infra/.env` (or exported in your shell). That is the only value you have to supply.
- No Bedrock model access is needed for this stack.

## Run it (one command, no manual steps)

```powershell
.\aws\bootstrap.ps1 -WhatIf        # optional: review the plan, makes no AWS call at all
.\aws\bootstrap.ps1                # create / update everything (first run builds + pushes the image; ~5-10 min)
.\aws\deploy-lambdas.ps1           # later, after changing code under aws/lambdas/src (or the model settings in config.ps1)
.\aws\teardown.ps1                 # remove the whole qs-or stack
```

```bash
./aws/bootstrap.sh --dry-run && ./aws/bootstrap.sh
./aws/deploy-lambdas.sh
./aws/teardown.sh
```

Both scripts accept `-EnvFile` / `--env-file PATH` (default `infra/.env`). Re-running bootstrap is safe; if you add or
change `OPENROUTER_API_KEY` later, just run it again and the key in SSM is overwritten.

What the bootstrap does, in order: S3 bucket and its policies, ECR repo and image build/push, the OpenRouter key into
SSM, the two IAM roles, the six log groups and Lambda functions (created, or updated incl. environment), reserved
concurrency (when the account allows), the state machine, the backend IAM user plus its access key, the budget, and
finally the `OR_*` lines in `infra/.env`.

## What bootstrap creates (all in us-east-1, profile `arpan-aws`)

- **S3** `qs-or-<account>-us-east-1`: Block Public Access, BucketOwnerEnforced, SSE-S3, deny-non-TLS policy, CORS
  for `http://localhost:3000` (+ `--cors-origin`), lifecycle `uploads/ raw/ batches/` 14 d, `derived/` 90 d, abort
  incomplete multipart uploads after 1 d.
- **ECR** `qs-or-worker` (scan on push, keep last 5 images) and the image build/push.
- **SSM Parameter Store** `/qs-or/openrouter-api-key`, type `SecureString` (default `aws/ssm` key, no CMK).
- **IAM** `qs-or-lambda-role` (S3: read `uploads/*`, read/write `raw/*` `derived/*`; `ssm:GetParameter` on that one
  parameter; its own log groups; no model-invoke permission at all), `qs-or-sfn-role` (`lambda:InvokeFunction` on the
  six functions only), user `qs-or-backend-app` (S3 put/get/abort/list on `uploads/ batches/ derived/`,
  `states:StartExecution` on the one state machine, `Describe/StopExecution` on its executions; no admin).
- **6 Lambda functions** `qs-or-ingest|extract-doc|plan-audio|transcribe-chunk|analyze-image|assemble` (container
  image, x86_64, 3008 MB, 900 s, no VPC, 10 GB `/tmp` for `ingest` and `plan-audio`), 14-day log groups, and reserved
  concurrency (3/6/6/12/12/3) only when the account has room (see Behaviour).
- **Step Functions** Standard state machine `qs-or-pipeline`.
- **AWS Budget** `qs-or-monthly`, $25/month, e-mail alerts at 80% actual and 100% forecast.
- **infra/.env** (git-ignored; created if missing; only the four `OR_*` keys are ever added or replaced, every other line
  is left alone): `OR_S3_BUCKET`, `OR_SFN_STATE_MACHINE_ARN` always; `OR_AWS_APP_ACCESS_KEY_ID` /
  `OR_AWS_APP_SECRET_ACCESS_KEY` only when the user has no access key yet. The secret is captured from the CLI and never
  printed. If a key exists but `.env` lacks the secret, run with `-RotateKey` / `--rotate-key`. The Bedrock stack's
  `S3_BUCKET`, `SFN_STATE_MACHINE_ARN`, `AWS_APP_*` lines are never read or modified. Teardown removes only the `OR_*` lines.

## OpenRouter models

All calls are `POST {OPENROUTER_BASE_URL}/chat/completions` with `Authorization: Bearer <key>`, `HTTP-Referer`,
`X-OpenRouter-Title` and `X-Title` headers. Models are tried in order; the next one is used when a model errors, refuses
or returns nothing. The lists are Lambda environment variables (comma separated), set from `config.*` (or from a process
env var of the same name when you run the scripts):

| Task | Variable | Default chain |
| --- | --- | --- |
| Transcribe 16 kHz mono mp3 chunks (`input_audio` content part, temperature 0, 8000 tokens) | `OPENROUTER_TRANSCRIBE_MODELS` | `mistralai/voxtral-small-24b-2507`, `google/gemini-2.5-flash` |
| OCR + describe images (`image_url` data URL, temperature 0, 3000 tokens) | `OPENROUTER_VISION_MODELS` | `openai/gpt-6-luna`, `deepseek/deepseek-v4.1-flash`, `google/gemini-2.5-flash` |

`OPENROUTER_BASE_URL` defaults to `https://openrouter.ai/api/v1`.

**Audio limitation:** neither `openai/gpt-6-luna` nor `deepseek/deepseek-v4.1-flash` accepts audio input on OpenRouter,
so they are in the vision chain only and must not be added to `OPENROUTER_TRANSCRIBE_MODELS`. Only audio-capable models
(Voxtral, Gemini, ...) belong there.

Errors: 429 / 408 / 5xx / connection errors are retried inside the call (up to 4 attempts, jittered exponential backoff,
timeouts connect 10 s / read 300 s, bounded by a time budget) and then raised as `TransientError`, which the state machine
retries. A 413 or a "too large" 400 makes the image analysis re-encode the image smaller and try again. 401 / 402 / 403
(bad key, no credits, forbidden) and a missing SSM parameter are permanent: the run fails with a clear message and the
key never appears in logs or error text. A response of HTTP 200 that carries an `error` object is classified by its code.
Token usage and the model used are recorded in the per-chunk sidecar JSON and the per-image analysis header.

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
- Reserved concurrency (42 in total for this stack) is applied only when the account's unreserved concurrency, plus what
  this stack already reserved, stays at or above `42 + 100` (AWS always keeps 100 unreserved). A second deployment in the
  same account doubles the reservations, so on a small quota the step is skipped with a message while everything else
  still works; request a Lambda concurrency quota increase and re-run to apply it.
- Google Docs are fetched as DOCX; Sheets and Slides as PDF. Folder listings are capped by Google at 50 files (warning).

## Tests

```bash
cd aws/lambdas && pip install -r requirements-dev.txt && python -m pytest -q
```

Local only, fakes only: no AWS and no network (a fake HTTP session stands in for OpenRouter and a fake SSM client for the
key lookup).

## Costs (rough, us-east-1)

Idle cost is about $0: Lambda, Step Functions Standard, S3, SSM standard parameters and Budgets are pay-per-use or free;
ECR storage is a few cents. Per evaluation the dominant costs are OpenRouter tokens (audio minutes through Voxtral /
Gemini, one vision call per image; billed by OpenRouter, not by AWS) and Lambda GB-seconds (3 GB x run time). A typical
submission (one 20-page PDF with 15 images and a 10-minute video) is on the order of cents to under a dollar; Step
Functions adds about $0.025 per 1,000 state transitions (a few hundred per evaluation). S3 is cents per month with the
14/90-day lifecycles. The $25/month AWS budget only sees AWS spend; watch your OpenRouter credit balance separately
(running out of credits shows up as a permanent 402 failure).

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
- No secrets in Lambda environment variables. The OpenRouter key lives in SSM Parameter Store as a SecureString; the
  Lambdas only get its parameter name (`OPENROUTER_SECRET_PARAM`), their role may `ssm:GetParameter` that single
  parameter only, and the code fetches it once per container and scrubs key-like strings from every log line and error.
  The bootstrap passes the key to the CLI through a temporary file (`--value file://`), never on the command line, and
  never prints it. The key is also present in plain text in your local git-ignored `infra/.env`, as it is for the backend.
- DNS rebinding between the resolve check and the connection is not fully closed (the targets are Google hosts and
  user input never chooses the host); the IAM role has no access to anything but the bucket prefixes and its one parameter.
- `qs-or-backend-app` can neither read `raw/*` nor read the SSM parameter nor touch other S3 buckets. Its key is in
  `infra/.env` only; rotate with `-RotateKey`.
