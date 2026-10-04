#!/usr/bin/env bash
# Rebuild the worker image, push it to ECR and point the six existing functions at it (update-function-code only).
# Infrastructure is NOT touched; run aws/bootstrap.sh for that.
#   ./aws/deploy-lambdas.sh --dry-run    print the planned commands only (no AWS calls)
set -euo pipefail
PROFILE="arpan-aws"; REGION="us-east-1"; PREFIX="qs-eval"; REPO="qs-eval-worker"
DRY=false
for a in "$@"; do
  case "$a" in --dry-run) DRY=true ;; -h|--help) sed -n '2,5p' "$0"; exit 0 ;; *) echo "Unknown option: $a" >&2; exit 2 ;; esac
done
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AWSCLI=(aws --profile "$PROFILE" --region "$REGION")
run() { if $DRY; then printf '  [plan] '; printf '%q ' "$@"; printf '\n'; else "$@"; fi; }

if $DRY; then ACCOUNT_ID="<ACCOUNT_ID>"; else ACCOUNT_ID="$("${AWSCLI[@]}" sts get-caller-identity --query Account --output text)"; fi
REGISTRY="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"
TAG="v$(date -u +%Y%m%d%H%M%S)"
IMAGE_URI="${REGISTRY}/${REPO}:${TAG}"

if $DRY; then
  echo "  [plan] aws --profile $PROFILE --region $REGION ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY"
else
  "${AWSCLI[@]}" ecr get-login-password | docker login --username AWS --password-stdin "$REGISTRY"
fi
run docker build --platform linux/amd64 --provenance=false -t "$IMAGE_URI" -t "${REGISTRY}/${REPO}:latest" "$HERE/lambdas"
run docker push "$IMAGE_URI"
run docker push "${REGISTRY}/${REPO}:latest"

for s in ingest extract-doc plan-audio transcribe-chunk analyze-image assemble; do
  run "${AWSCLI[@]}" lambda update-function-code --function-name "${PREFIX}-$s" --image-uri "$IMAGE_URI"
  if $DRY; then echo "  [plan] aws lambda wait function-updated-v2 --function-name ${PREFIX}-$s"; else "${AWSCLI[@]}" lambda wait function-updated-v2 --function-name "${PREFIX}-$s"; fi
done
echo "Deployed $IMAGE_URI to 6 functions."
