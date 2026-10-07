#!/usr/bin/env bash
# Idempotent (describe-or-create) provisioning of the OpenRouter variant of the AI evaluation pipeline
# (separate "qs-or" stack; never touches the main-branch Bedrock deployment). Names come from aws/config.sh.
#
#   ./aws/bootstrap.sh --dry-run          print the planned commands only; makes NO AWS call and changes nothing
#   ./aws/bootstrap.sh                    create / update everything
#
# Options:
#   --dry-run              print planned commands only (no AWS calls at all, not even read-only ones)
#   --cors-origin URL      extra browser origin for S3 CORS (http://localhost:3000 is always allowed)
#   --email ADDRESS        budget alert address (default arpankumar1119@gmail.com)
#   --skip-image           reuse the newest image already in ECR instead of building/pushing
#   --rotate-key           delete the existing qs-or-backend-app access key(s) and create a new one
#   --env-file PATH        env file to read OPENROUTER_API_KEY from and write the OR_* variables to (default infra/.env)
#   --profile NAME         AWS CLI profile (default arpan-aws)
#
# The OpenRouter API key is read from $OPENROUTER_API_KEY, then from OPENROUTER_API_KEY= in the env file, and is
# stored in SSM Parameter Store (SecureString). It is never printed and never placed in Lambda env vars.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
ENV_FILE="$REPO_ROOT/infra/.env"
DRY=false
CORS_ORIGIN=""
BUDGET_EMAIL="arpankumar1119@gmail.com"
SKIP_IMAGE=false
ROTATE_KEY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=true ;;
    --cors-origin) CORS_ORIGIN="${2:?--cors-origin needs a value}"; shift ;;
    --email) BUDGET_EMAIL="${2:?--email needs a value}"; shift ;;
    --skip-image) SKIP_IMAGE=true ;;
    --rotate-key) ROTATE_KEY=true ;;
    --env-file) ENV_FILE="${2:?--env-file needs a value}"; shift ;;
    --profile) PROFILE="${2:?--profile needs a value}"; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

AWSCLI=(aws --profile "$PROFILE" --region "$REGION")
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\n==> %s\n' "$*"; }
plan() { printf '  [plan] %s\n' "$*"; }

# run: a mutating command. In dry-run it is only printed.
run() {
  if $DRY; then printf '  [plan] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
# aws_run: mutating AWS CLI call with the profile/region prefix.
aws_run() { run "${AWSCLI[@]}" "$@"; }
# check: read-only AWS call whose exit status says "exists". In dry-run nothing is called and it reports "missing".
check() {
  if $DRY; then printf '  [plan] (check) aws %s\n' "$*"; return 1; fi
  "${AWSCLI[@]}" "$@" >/dev/null 2>&1
}
# query: read-only AWS call returning text. In dry-run returns the fallback ($1) without calling AWS.
query() {
  local fallback="$1"; shift
  if $DRY; then printf '  [plan] (read) aws %s\n' "$*" >&2; printf '%s' "$fallback"; return 0; fi
  "${AWSCLI[@]}" "$@" 2>/dev/null || printf '%s' "$fallback"
}

# ------------------------------------------------------------------ account + names
if $DRY; then
  ACCOUNT_ID="<ACCOUNT_ID>"
  echo "DRY RUN: nothing below is executed. No AWS call (not even read-only) is made."
else
  command -v aws >/dev/null || { echo "aws CLI not found" >&2; exit 1; }
  ACCOUNT_ID="$("${AWSCLI[@]}" sts get-caller-identity --query Account --output text)"
fi
BUCKET="${PREFIX}-${ACCOUNT_ID}-${REGION}"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
REPO_URI="${REGISTRY}/${REPO}"
LAMBDA_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${LAMBDA_ROLE}"
SFN_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${SFN_ROLE}"
SM_ARN="arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${STATE_MACHINE}"
fn_arn() { echo "arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${PREFIX}-$1"; }
echo "stack=$PREFIX profile=$PROFILE account=$ACCOUNT_ID region=$REGION bucket=$BUCKET"

# render TEMPLATE -> $WORK/name (substitute the ${...} placeholders)
render() {
  local src="$1" dst="$WORK/$(basename "$1")"
  sed -e "s|\${ACCOUNT_ID}|${ACCOUNT_ID}|g" -e "s|\${REGION}|${REGION}|g" -e "s|\${BUCKET}|${BUCKET}|g" \
      -e "s|\${BUDGET_EMAIL}|${BUDGET_EMAIL}|g" -e "s|\${PREFIX}|${PREFIX}|g" -e "s|\${APP_USER}|${APP_USER}|g" \
      -e "s|\${STATE_MACHINE}|${STATE_MACHINE}|g" -e "s|\${BUDGET_NAME}|${BUDGET_NAME}|g" \
      -e "s|\${SSM_PARAM}|${SSM_PARAM}|g" "$src" > "$dst"
  echo "$dst"
}

# ------------------------------------------------------------------ env file helpers (never print values)
set_env() { # KEY VALUE -> env file (replace or append), never echoes VALUE
  local key="$1" val="$2" tmp
  tmp="$(mktemp)"
  { [[ -f "$ENV_FILE" ]] && grep -v "^${key}=" "$ENV_FILE" || true; } > "$tmp"
  printf '%s=%s\n' "$key" "$val" >> "$tmp"
  mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE" 2>/dev/null || true
}
env_has() { [[ -f "$ENV_FILE" ]] && grep -q "^$1=." "$ENV_FILE"; }
# OPENROUTER_API_KEY: process env first, then the env file. Sets OR_KEY (empty if none) and OR_KEY_SOURCE.
read_openrouter_key() {
  OR_KEY=""; OR_KEY_SOURCE=""
  if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    OR_KEY="$(printf '%s' "$OPENROUTER_API_KEY" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    OR_KEY_SOURCE="the OPENROUTER_API_KEY environment variable"
  fi
  if [[ -z "$OR_KEY" && -f "$ENV_FILE" ]]; then
    local line v
    line="$(grep -E '^[[:space:]]*(export[[:space:]]+)?OPENROUTER_API_KEY[[:space:]]*=' "$ENV_FILE" | tail -n 1 | tr -d '\r' || true)"
    if [[ -n "$line" ]]; then
      v="${line#*=}"
      v="$(printf '%s' "$v" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
      if [[ "$v" =~ ^\"([^\"]*)\" ]] || [[ "$v" =~ ^\'([^\']*)\' ]] || [[ "$v" =~ ^([^[:space:]]*) ]]; then v="${BASH_REMATCH[1]}"; fi
      OR_KEY="$v"; OR_KEY_SOURCE="$ENV_FILE"
    fi
  fi
}

# ------------------------------------------------------------------ 1. S3
say "S3 bucket $BUCKET"
if check s3api head-bucket --bucket "$BUCKET"; then
  echo "  bucket exists"
else
  if [[ "$REGION" == "us-east-1" ]]; then
    aws_run s3api create-bucket --bucket "$BUCKET"
  else
    aws_run s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration "LocationConstraint=$REGION"
  fi
fi
aws_run s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws_run s3api put-bucket-ownership-controls --bucket "$BUCKET" \
  --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
aws_run s3api put-bucket-encryption --bucket "$BUCKET" \
  --server-side-encryption-configuration "file://$(render "$HERE/policies/bucket-encryption.json")"
aws_run s3api put-bucket-policy --bucket "$BUCKET" --policy "file://$(render "$HERE/policies/bucket-policy.json")"
CORS_FILE="$(render "$HERE/policies/bucket-cors.json")"
if [[ -n "$CORS_ORIGIN" ]]; then
  sed -i.bak "s|\"http://localhost:3000\"|\"http://localhost:3000\", \"${CORS_ORIGIN}\"|" "$CORS_FILE" && rm -f "$CORS_FILE.bak"
fi
aws_run s3api put-bucket-cors --bucket "$BUCKET" --cors-configuration "file://$CORS_FILE"
aws_run s3api put-bucket-lifecycle-configuration --bucket "$BUCKET" \
  --lifecycle-configuration "file://$(render "$HERE/policies/bucket-lifecycle.json")"

# ------------------------------------------------------------------ 2. ECR + image
say "ECR repository $REPO"
if check ecr describe-repositories --repository-names "$REPO"; then
  echo "  repository exists"
else
  aws_run ecr create-repository --repository-name "$REPO" \
    --image-scanning-configuration scanOnPush=true --encryption-configuration encryptionType=AES256
fi
aws_run ecr put-lifecycle-policy --repository-name "$REPO" --lifecycle-policy-text "file://$(render "$HERE/policies/ecr-lifecycle.json")"
aws_run ecr set-repository-policy --repository-name "$REPO" --policy-text "file://$(render "$HERE/policies/ecr-repo-policy.json")"

TAG="v$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${REPO_URI}:${TAG}"
if $SKIP_IMAGE; then
  say "Reusing the newest image in ECR (--skip-image)"
  IMAGE_URI="${REPO_URI}:latest"
else
  say "Build and push image $IMAGE_URI (linux/amd64, no provenance attestation so Lambda accepts it)"
  if $DRY; then
    plan "aws --profile $PROFILE --region $REGION ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY"
  else
    "${AWSCLI[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
  fi
  run docker build --platform linux/amd64 --provenance=false -t "$IMAGE_URI" -t "${REPO_URI}:latest" "$HERE/lambdas"
  run docker push "$IMAGE_URI"
  run docker push "${REPO_URI}:latest"
fi

# ------------------------------------------------------------------ 3. OpenRouter key -> SSM SecureString
say "OpenRouter API key -> SSM SecureString $SSM_PARAM"
KEY_STORED=false
read_openrouter_key
if [[ -n "$OR_KEY" ]]; then
  KEY_FILE="$WORK/openrouter-key.txt"
  (umask 077; printf '%s' "$OR_KEY" > "$KEY_FILE")   # no trailing newline; removed with $WORK on exit
  echo "  key found in $OR_KEY_SOURCE (value not printed)"
  aws_run ssm put-parameter --name "$SSM_PARAM" --type SecureString --overwrite --value "file://$KEY_FILE" \
    --description "OpenRouter API key for the $PREFIX pipeline Lambdas"
  KEY_STORED=true
  OR_KEY=""
elif check ssm get-parameter --name "$SSM_PARAM"; then
  echo "  WARNING: no OPENROUTER_API_KEY in the environment or in $ENV_FILE; keeping the key already stored in SSM."
else
  echo "  WARNING: no OPENROUTER_API_KEY found (environment variable or $ENV_FILE) and $SSM_PARAM does not exist yet."
  echo "           The Lambdas are still deployed but audio/image analysis will fail until the key is stored."
  echo "           Add  OPENROUTER_API_KEY=...  to $ENV_FILE and re-run ./aws/bootstrap.sh (idempotent)."
fi

# ------------------------------------------------------------------ 4. IAM roles
ensure_role() { # name trust-file policy-name policy-file description
  local name="$1" trust="$2" pname="$3" pfile="$4" desc="$5"
  if check iam get-role --role-name "$name"; then
    echo "  role $name exists (refreshing trust + inline policy)"
    aws_run iam update-assume-role-policy --role-name "$name" --policy-document "file://$trust"
    ROLE_CREATED=false
  else
    aws_run iam create-role --role-name "$name" --assume-role-policy-document "file://$trust" --description "$desc"
    ROLE_CREATED=true
  fi
  aws_run iam put-role-policy --role-name "$name" --policy-name "$pname" --policy-document "file://$pfile"
}
say "IAM role $LAMBDA_ROLE"
ensure_role "$LAMBDA_ROLE" "$(render "$HERE/policies/lambda-trust.json")" "$LAMBDA_POLICY_NAME" \
  "$(render "$HERE/policies/lambda-role-policy.json")" "$PREFIX Lambda execution role (S3 prefixes, one SSM parameter, logs)"
NEW_ROLES=$ROLE_CREATED
say "IAM role $SFN_ROLE"
ensure_role "$SFN_ROLE" "$(render "$HERE/policies/sfn-trust.json")" "$SFN_POLICY_NAME" \
  "$(render "$HERE/policies/sfn-role-policy.json")" "$PREFIX Step Functions role (invoke the six functions only)"
[[ "$ROLE_CREATED" == true ]] && NEW_ROLES=true
if [[ "$NEW_ROLES" == true ]]; then
  say "Waiting 15s for new IAM roles to propagate"
  if $DRY; then plan "sleep 15"; else sleep 15; fi
fi

# ------------------------------------------------------------------ 5. Lambda functions
# name | handler | memory MB | ephemeral MB | reserved concurrency
FUNCTIONS=(
  "ingest|worker.handlers.ingest|3008|10240|3"
  "extract-doc|worker.handlers.extract_doc|3008|2048|6"
  "plan-audio|worker.handlers.plan_audio|3008|10240|6"
  "transcribe-chunk|worker.handlers.transcribe_chunk|3008|512|12"
  "analyze-image|worker.handlers.analyze_image|3008|512|12"
  "assemble|worker.handlers.assemble|3008|512|3"
)
wait_fn() { # state: function-active-v2 | function-updated-v2
  if $DRY; then plan "aws lambda wait $1 --function-name $2"; return 0; fi
  "${AWSCLI[@]}" lambda wait "$1" --function-name "$2"
}

ENV_JSON="file://$(write_lambda_env_json "$WORK")"
say "Log groups (14-day retention) and Lambda functions"
TOTAL_RESERVED=0
for spec in "${FUNCTIONS[@]}"; do
  IFS='|' read -r short handler mem eph reserved <<<"$spec"
  fn="${PREFIX}-${short}"
  lg="/aws/lambda/${fn}"
  TOTAL_RESERVED=$((TOTAL_RESERVED + reserved))
  if [[ "$(query "" logs describe-log-groups --log-group-name-prefix "$lg" --query "length(logGroups[?logGroupName=='$lg'])" --output text)" == "1" ]]; then
    echo "  log group $lg exists"
  else
    aws_run logs create-log-group --log-group-name "$lg"
  fi
  aws_run logs put-retention-policy --log-group-name "$lg" --retention-in-days 14

  img_cfg="{\"Command\":[\"${handler}\"]}"
  if check lambda get-function --function-name "$fn"; then
    echo "  function $fn exists (updating configuration + environment + code)"
    aws_run lambda update-function-configuration --function-name "$fn" --role "$LAMBDA_ROLE_ARN" --timeout 900 \
      --memory-size "$mem" --ephemeral-storage "Size=$eph" --image-config "$img_cfg" --environment "$ENV_JSON"
    wait_fn function-updated-v2 "$fn"
    aws_run lambda update-function-code --function-name "$fn" --image-uri "$IMAGE_URI"
    wait_fn function-updated-v2 "$fn"
  else
    created=false
    for attempt in 1 2 3 4; do
      if $DRY; then
        aws_run lambda create-function --function-name "$fn" --package-type Image --code "ImageUri=$IMAGE_URI" \
          --role "$LAMBDA_ROLE_ARN" --timeout 900 --memory-size "$mem" --ephemeral-storage "Size=$eph" \
          --architectures x86_64 --image-config "$img_cfg" --environment "$ENV_JSON" \
          --description "$PREFIX pipeline: $short"
        created=true; break
      fi
      if "${AWSCLI[@]}" lambda create-function --function-name "$fn" --package-type Image --code "ImageUri=$IMAGE_URI" \
          --role "$LAMBDA_ROLE_ARN" --timeout 900 --memory-size "$mem" --ephemeral-storage "Size=$eph" \
          --architectures x86_64 --image-config "$img_cfg" --environment "$ENV_JSON" \
          --description "$PREFIX pipeline: $short" >/dev/null; then
        created=true; break
      fi
      echo "  create-function failed (IAM propagation?); retrying in 10s ($attempt/4)"; sleep 10
    done
    $created || { echo "Could not create $fn" >&2; exit 1; }
    wait_fn function-active-v2 "$fn"
  fi
done

# Account-level guard. AWS always keeps 100 unreserved executions, and a new account's limit is only 10, so
# reservations are applied only where the account can afford them. The unreserved figure already excludes what THIS
# stack reserved on an earlier run, so that amount is added back (keeps re-runs idempotent). A second deployment in the
# same account (e.g. the main-branch stack + qs-or) needs another 42 on top of the first stack's: when the account cannot afford it
# the step is skipped with a message and everything else still works.
say "Reserved concurrency guard (only where the account allows; AWS keeps 100 unreserved)"
UNRESERVED="$(query 0 lambda get-account-settings --query 'AccountLimit.UnreservedConcurrentExecutions' --output text)"
OWN_RESERVED=0
for spec in "${FUNCTIONS[@]}"; do
  IFS='|' read -r short _h _m _e _r <<<"$spec"
  cur="$(query 0 lambda get-function-concurrency --function-name "${PREFIX}-${short}" --query ReservedConcurrentExecutions --output text)"
  [[ "$cur" =~ ^[0-9]+$ ]] && OWN_RESERVED=$((OWN_RESERVED + cur))
done
if $DRY || [[ "$UNRESERVED" =~ ^[0-9]+$ && $((UNRESERVED + OWN_RESERVED)) -ge $((TOTAL_RESERVED + 100)) ]]; then
  for spec in "${FUNCTIONS[@]}"; do
    IFS='|' read -r short _h _m _e reserved <<<"$spec"
    aws_run lambda put-function-concurrency --function-name "${PREFIX}-${short}" --reserved-concurrent-executions "$reserved"
  done
else
  echo "  SKIPPED: unreserved concurrency is $UNRESERVED (+$OWN_RESERVED already reserved by $PREFIX), need >= $((TOTAL_RESERVED + 100)). Request a Lambda concurrency quota increase, then re-run. Everything else works without reservations."
fi

# ------------------------------------------------------------------ 6. State machine
say "Step Functions state machine $STATE_MACHINE"
ASL="$WORK/statemachine.asl.json"
sed -e "s|\${IngestFnArn}|$(fn_arn ingest)|g" -e "s|\${ExtractDocFnArn}|$(fn_arn extract-doc)|g" \
    -e "s|\${PlanAudioFnArn}|$(fn_arn plan-audio)|g" -e "s|\${TranscribeChunkFnArn}|$(fn_arn transcribe-chunk)|g" \
    -e "s|\${AnalyzeImageFnArn}|$(fn_arn analyze-image)|g" -e "s|\${AssembleFnArn}|$(fn_arn assemble)|g" \
    "$HERE/statemachine.asl.json" > "$ASL"
if check stepfunctions describe-state-machine --state-machine-arn "$SM_ARN"; then
  aws_run stepfunctions update-state-machine --state-machine-arn "$SM_ARN" --definition "file://$ASL" --role-arn "$SFN_ROLE_ARN"
else
  aws_run stepfunctions create-state-machine --name "$STATE_MACHINE" --type STANDARD --definition "file://$ASL" --role-arn "$SFN_ROLE_ARN"
fi

# ------------------------------------------------------------------ 7. Backend IAM user + key
say "IAM user $APP_USER"
if check iam get-user --user-name "$APP_USER"; then
  echo "  user exists"
else
  aws_run iam create-user --user-name "$APP_USER"
fi
aws_run iam put-user-policy --user-name "$APP_USER" --policy-name "$APP_USER_POLICY_NAME" \
  --policy-document "file://$(render "$HERE/policies/backend-user-policy.json")"

say "Access key for $APP_USER and $ENV_FILE"
if $DRY; then
  plan "(check) aws iam list-access-keys --user-name $APP_USER"
  plan "if the user has NO access key: aws iam create-access-key --user-name $APP_USER  (output captured, not printed)"
  plan "write ${ENV_KEYS[0]}, ${ENV_KEYS[1]} (always) and ${ENV_KEYS[2]} / ${ENV_KEYS[3]} (only if a key was created) to $ENV_FILE; no other variable is touched"
  plan "with --rotate-key: aws iam delete-access-key for existing keys first"
else
  rc=0; (cd "$REPO_ROOT" && git check-ignore -q "$ENV_FILE") || rc=$?
  if [[ $rc -eq 1 ]]; then echo "$ENV_FILE is not git-ignored; refusing to write secrets" >&2; exit 1; fi
  [[ -f "$ENV_FILE" ]] || : > "$ENV_FILE"
  set_env OR_S3_BUCKET "$BUCKET"
  set_env OR_SFN_STATE_MACHINE_ARN "$SM_ARN"
  if $ROTATE_KEY; then
    for kid in $("${AWSCLI[@]}" iam list-access-keys --user-name "$APP_USER" --query 'AccessKeyMetadata[].AccessKeyId' --output text); do
      "${AWSCLI[@]}" iam delete-access-key --user-name "$APP_USER" --access-key-id "$kid"
    done
  fi
  NKEYS="$("${AWSCLI[@]}" iam list-access-keys --user-name "$APP_USER" --query 'length(AccessKeyMetadata)' --output text)"
  if [[ "$NKEYS" == "0" ]]; then
    CREDS="$("${AWSCLI[@]}" iam create-access-key --user-name "$APP_USER" --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text)"
    read -r AKID SECRET <<<"$CREDS"
    set_env OR_AWS_APP_ACCESS_KEY_ID "$AKID"
    set_env OR_AWS_APP_SECRET_ACCESS_KEY "$SECRET"
    unset CREDS AKID SECRET
    echo "  created a new access key and stored it in $ENV_FILE (not printed)"
  elif env_has OR_AWS_APP_SECRET_ACCESS_KEY; then
    echo "  access key already exists and $ENV_FILE has credentials; left unchanged"
  else
    echo "  WARNING: $APP_USER already has an access key but $ENV_FILE has no secret (it cannot be retrieved)."
    echo "           Re-run with --rotate-key to replace it."
  fi
fi

# ------------------------------------------------------------------ 8. Budget
say "AWS Budget $BUDGET_NAME (\$25/month, alerts to $BUDGET_EMAIL)"
if check budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME"; then
  echo "  budget exists"
else
  aws_run budgets create-budget --account-id "$ACCOUNT_ID" --budget "file://$(render "$HERE/policies/budget.json")" \
    --notifications-with-subscribers "file://$(render "$HERE/policies/budget-notifications.json")"
fi

say "Done"
if $DRY; then
  echo "Dry run finished: no AWS call was made."
else
  echo "Bucket:        $BUCKET"
  echo "State machine: $SM_ARN"
  echo "SSM parameter: $SSM_PARAM (key stored this run: $KEY_STORED)"
  echo "OR_* credentials and ids are in $ENV_FILE. Update code later with aws/deploy-lambdas.sh."
  $KEY_STORED || echo "NOTE: add OPENROUTER_API_KEY to $ENV_FILE and re-run this script to store/refresh the key in SSM."
fi
