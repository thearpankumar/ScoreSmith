#!/usr/bin/env bash
# Rebuild the worker image, push it to ECR and point the six existing functions at it. Before each code update the
# function environment (LOG_LEVEL, OPENROUTER_SECRET_PARAM) is re-applied, and a non-empty OPENROUTER_API_KEY in
# infra/.env is copied to the SSM SecureString the Lambdas read. Other infrastructure is NOT touched; run
# aws/bootstrap.sh for that.
#   ./aws/deploy-lambdas.sh --dry-run    print the planned commands only (no AWS calls)
set -euo pipefail
PROFILE="arpan-aws"; REGION="us-east-1"; PREFIX="qs-eval"; REPO="qs-eval-worker"
SSM_PARAM="/${PREFIX}/openrouter-api-key"
DRY=false
for a in "$@"; do
  case "$a" in --dry-run) DRY=true ;; -h|--help) sed -n '2,6p' "$0"; exit 0 ;; *) echo "Unknown option: $a" >&2; exit 2 ;; esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$(cd "$HERE/.." && pwd)/infra/.env"
AWSCLI=(aws --profile "$PROFILE" --region "$REGION")
# Git Bash / MSYS rewrites every argument that starts with "/" into a Windows path before aws.exe sees it
# (/qs-eval/... -> C:/Program Files/Git/qs-eval/...). Exempt the SSM parameter and log-group names only; other
# paths (docker build context, file:// work files) still convert. No effect outside MSYS.
export MSYS2_ARG_CONV_EXCL="/${PREFIX}/;/aws/lambda/"
WORK="$(mktemp -d)"
# Git Bash / MSYS: aws.exe needs the C:/... form of the temp dir in file:// arguments (see bootstrap.sh).
if command -v cygpath >/dev/null 2>&1; then WORK="$(cygpath -m "$WORK")"; fi
trap 'rm -rf "$WORK"' EXIT
run() { if $DRY; then printf '  [plan] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi; }
plan() { printf '  [plan] %s\n' "$*"; }
check() {  # read-only; in dry-run nothing is called and it reports "missing"
  if $DRY; then printf '  [plan] (check) aws %s\n' "$*"; return 1; fi
  "${AWSCLI[@]}" "$@" >/dev/null 2>&1
}

# Value of KEY in infra/.env; the last occurrence wins (as in docker compose), surrounding quotes and a trailing
# " # comment" are dropped. Empty when absent. Callers must never print it. (Same as in bootstrap.sh.)
env_value() {
  [[ -f "$ENV_FILE" ]] || return 0
  local line v
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" "$ENV_FILE" | tail -n 1 | tr -d '\r')" || true
  [[ -n "$line" ]] || return 0
  v="${line#*=}"
  v="${v#"${v%%[![:space:]]*}"}"
  if [[ "$v" =~ ^\"([^\"]*)\" ]] || [[ "$v" =~ ^\'([^\']*)\' ]] || [[ "$v" =~ ^([^[:space:]]*) ]]; then v="${BASH_REMATCH[1]}"; fi
  printf '%s' "$v"
}

# OPENROUTER_API_KEY from infra/.env -> SSM SecureString, through a private temp file (never on a command line).
sync_openrouter_key() {
  local key f="$WORK/openrouter-key.txt"
  key="$(env_value OPENROUTER_API_KEY)"
  if [[ -n "$key" ]]; then
    if $DRY; then
      plan "${AWSCLI[*]} ssm put-parameter --name $SSM_PARAM --type SecureString --overwrite --value file://<temp file with OPENROUTER_API_KEY from $ENV_FILE>"
    else
      (umask 077; printf '%s' "$key" > "$f")
      "${AWSCLI[@]}" ssm put-parameter --name "$SSM_PARAM" --type SecureString --overwrite --value "file://$f" \
        --description "OpenRouter API key for the $PREFIX pipeline Lambdas" >/dev/null
      rm -f "$f"
      echo "  stored OPENROUTER_API_KEY from $ENV_FILE in $SSM_PARAM (value not printed)"
    fi
  elif check ssm get-parameter --name "$SSM_PARAM"; then
    echo "  no OPENROUTER_API_KEY in $ENV_FILE; keeping the key already stored in $SSM_PARAM"
  else
    echo "  NOTE: no OPENROUTER_API_KEY in $ENV_FILE and $SSM_PARAM does not exist. Evaluations that use OpenRouter"
    echo "        fail fast (at ingest) until it is set: add the key to $ENV_FILE and re-run this script."
  fi
  key=""
}

if $DRY; then ACCOUNT_ID="<ACCOUNT_ID>"; else ACCOUNT_ID="$("${AWSCLI[@]}" sts get-caller-identity --query Account --output text)"; fi
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
TAG="v$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${REGISTRY}/${REPO}:${TAG}"

echo "==> OpenRouter API key -> SSM SecureString $SSM_PARAM"
sync_openrouter_key

# The Lambda role's inline policy, re-applied so a stack bootstrapped before the OpenRouter change gets
# ssm:GetParameter on the key parameter (same rendering as bootstrap.sh).
echo "==> IAM inline policy ${PREFIX}-lambda-inline on ${PREFIX}-lambda-role"
ROLE_POLICY="$WORK/lambda-role-policy.json"
sed -e "s|\${ACCOUNT_ID}|${ACCOUNT_ID}|g" -e "s|\${REGION}|${REGION}|g" -e "s|\${BUCKET}|${PREFIX}-${ACCOUNT_ID}-${REGION}|g" \
  "$HERE/policies/lambda-role-policy.json" > "$ROLE_POLICY"
run "${AWSCLI[@]}" iam put-role-policy --role-name "${PREFIX}-lambda-role" --policy-name "${PREFIX}-lambda-inline" \
  --policy-document "file://$ROLE_POLICY"

echo "==> Build and push $IMAGE_URI"
if $DRY; then
  echo "  [plan] aws --profile $PROFILE --region $REGION ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY"
else
  "${AWSCLI[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
fi
run docker build --platform linux/amd64 --provenance=false -t "$IMAGE_URI" -t "${REGISTRY}/${REPO}:latest" "$HERE/lambdas"
run docker push "$IMAGE_URI"
run docker push "${REGISTRY}/${REPO}:latest"

# The function environment (the SSM parameter name only, never the key), then the code.
LAMBDA_ENV="$WORK/lambda-env.json"
printf '{"Variables":{"LOG_LEVEL":"INFO","OPENROUTER_SECRET_PARAM":"%s"}}\n' "$SSM_PARAM" > "$LAMBDA_ENV"
wait_updated() {
  if $DRY; then echo "  [plan] ${AWSCLI[*]} lambda wait function-updated-v2 --function-name $1"; else "${AWSCLI[@]}" lambda wait function-updated-v2 --function-name "$1"; fi
}
echo "==> Lambda functions (environment + code)"
for s in ingest extract-doc plan-audio transcribe-chunk analyze-image assemble; do
  run "${AWSCLI[@]}" lambda update-function-configuration --function-name "${PREFIX}-$s" --environment "file://$LAMBDA_ENV"
  wait_updated "${PREFIX}-$s"
  run "${AWSCLI[@]}" lambda update-function-code --function-name "${PREFIX}-$s" --image-uri "$IMAGE_URI"
  wait_updated "${PREFIX}-$s"
done
echo "Deployed $IMAGE_URI to 6 functions (environment + code)."
