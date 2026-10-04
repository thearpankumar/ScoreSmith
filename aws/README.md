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
| `deploy-lambdas.ps1` / `.sh` | Rebuild the image, push, `update-function-code` for the six functions. Nothing else. |
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
  regional foundation models; its own log groups), `qs-eval-sfn-role` (`lambda:InvokeFunction` on the six functions
  only), user `qs-backend-app` (S3 put/get/abort/list on `uploads/ batches/ derived/`, `states:StartExecution` on the one
  state machine, `Describe/StopExecution` on its executions; no Bedrock, no admin).
- **6 Lambda functions** `qs-eval-ingest|extract-doc|plan-audio|transcribe-chunk|analyze-image|assemble` (container
  image, x86_64, 3008 MB, 900 s, no VPC, 10 GB `/tmp` for `ingest` and `plan-audio`), 14-day log groups, and reserved
  concurrency (3/6/6/12/12/3) only when the account has >= 100 + 42 unreserved.
- **Step Functions** Standard state machine `qs-eval-pipeline`.
- **AWS Budget** `qs-eval-monthly`, $25/month, e-mail alerts at 80% actual and 100% forecast.
- **infra/.env** (git-ignored): `S3_BUCKET`, `SFN_STATE_MACHINE_ARN` always; `AWS_APP_ACCESS_KEY_ID` /
  `AWS_APP_SECRET_ACCESS_KEY` only when the user has no access key yet. The secret is captured from the CLI and never
  printed. If a key exists but `.env` lacks the secret, run with `-RotateKey` / `--rotate-key`.

## Running it

Prerequisites: AWS CLI v2 with profile `arpan-aws`, Docker (Linux images; Docker Desktop on Windows is fine), and
Bedrock model access already granted for the five models.

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

Lambda unit tests (local, fakes only):

```bash
cd aws/lambdas && pip install -r requirements-dev.txt && python -m pytest -q
```

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
- Models: Voxtral small then mini; Kimi K2.5, then Qwen3-VL, then Llama 4 Maverick. Auth is the Lambda role.

## Costs (rough, us-east-1)

Idle cost is about $0: Lambda, Step Functions Standard, S3 and Budgets are pay-per-use; ECR storage is a few cents.
Per evaluation the dominant costs are Bedrock tokens (Voxtral per audio minute, vision model per image) and Lambda
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
- No secrets in Lambda environment variables; Bedrock access is by role and limited to the named model ARNs.
- DNS rebinding between the resolve check and the connection is not fully closed (the targets are Google hosts and
  user input never chooses the host); the IAM role has no access to anything but the bucket prefixes and the models.
- `qs-backend-app` can neither read `raw/*` nor invoke Bedrock nor touch other S3 buckets. Its key is in `infra/.env`
  only; rotate with `-RotateKey`.
