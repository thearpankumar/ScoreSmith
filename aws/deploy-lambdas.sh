#!/usr/bin/env bash
# Rebuild the worker image, push it to ECR, point the six existing qs-or functions at it and (re)apply their
# environment (OPENROUTER_* settings) so config changes in aws/config.sh reach already-deployed functions.
# Other infrastructure is NOT touched; run aws/bootstrap.sh for that (it also stores the OpenRouter key in SSM).
#   ./aws/deploy-lambdas.sh --dry-run    print the planned commands only (no AWS calls)
#   ./aws/deploy-lambdas.sh --profile NAME
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
DRY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=true ;;
    --profile) PROFILE="${2:?--profile needs a value}"; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
AWSCLI=(aws --profile "$PROFILE" --region "$REGION")
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
run() { if $DRY; then printf '  [plan] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi; }

if $DRY; then ACCOUNT_ID="<ACCOUNT_ID>"; else ACCOUNT_ID="$("${AWSCLI[@]}" sts get-caller-identity --query Account --output text)"; fi
echo "stack=$PREFIX profile=$PROFILE account=$ACCOUNT_ID region=$REGION"
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
TAG="v$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${REGISTRY}/${REPO}:${TAG}"
ENV_JSON="file://$(write_lambda_env_json "$WORK")"

if $DRY; then
  echo "  [plan] aws --profile $PROFILE --region $REGION ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY"
else
  "${AWSCLI[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
fi
run docker build --platform linux/amd64 --provenance=false -t "$IMAGE_URI" -t "${REGISTRY}/${REPO}:latest" "$HERE/lambdas"
run docker push "$IMAGE_URI"
run docker push "${REGISTRY}/${REPO}:latest"

wait_updated() { if $DRY; then echo "  [plan] aws lambda wait function-updated-v2 --function-name $1"; else "${AWSCLI[@]}" lambda wait function-updated-v2 --function-name "$1"; fi; }
for s in "${FUNCTION_SHORTS[@]}"; do
  fn="${PREFIX}-$s"
  run "${AWSCLI[@]}" lambda update-function-code --function-name "$fn" --image-uri "$IMAGE_URI"
  wait_updated "$fn"
  run "${AWSCLI[@]}" lambda update-function-configuration --function-name "$fn" --environment "$ENV_JSON"
  wait_updated "$fn"
done
echo "Deployed $IMAGE_URI to ${#FUNCTION_SHORTS[@]} functions (code + environment)."
