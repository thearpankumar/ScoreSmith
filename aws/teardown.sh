#!/usr/bin/env bash
# Remove EVERYTHING that aws/bootstrap.sh created (bucket contents included, irreversibly).
#   ./aws/teardown.sh --dry-run    print the planned commands only (no AWS calls)
#   ./aws/teardown.sh              asks you to type the bucket name to confirm
#   ./aws/teardown.sh --yes        skip the prompt (scripts / CI)
set -uo pipefail

PROFILE="arpan-aws"; REGION="us-east-1"; PREFIX="qs-eval"
DRY=false; YES=false
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=true ;; --yes) YES=true ;;
    -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$(cd "$HERE/.." && pwd)/infra/.env"
AWSCLI=(aws --profile "$PROFILE" --region "$REGION")
run() {  # mutating; failures (already gone) are reported but do not stop the teardown
  if $DRY; then printf '  [plan] '; printf '%q ' "$@"; printf '\n'; return 0; fi
  "$@" >/dev/null 2>&1 && echo "  ok: $*" || echo "  (skipped, probably already gone): $*"
}
awsr() { run "${AWSCLI[@]}" "$@"; }

if $DRY; then ACCOUNT_ID="<ACCOUNT_ID>"; else ACCOUNT_ID="$("${AWSCLI[@]}" sts get-caller-identity --query Account --output text)" || exit 1; fi
BUCKET="${PREFIX}-${ACCOUNT_ID}-${REGION}"
SM_ARN="arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${PREFIX}-pipeline"

echo "This will PERMANENTLY delete in account $ACCOUNT_ID ($REGION):"
echo "  S3 bucket $BUCKET and ALL its objects, ECR repo ${PREFIX}-worker, 6 Lambda functions + log groups,"
echo "  state machine ${PREFIX}-pipeline, IAM roles ${PREFIX}-lambda-role / ${PREFIX}-sfn-role, IAM user qs-backend-app (+keys),"
echo "  budget ${PREFIX}-monthly, and the related lines in infra/.env."
if ! $DRY && ! $YES; then
  read -r -p "Type the bucket name ($BUCKET) to continue: " answer
  [[ "$answer" == "$BUCKET" ]] || { echo "Aborted."; exit 1; }
fi

echo; echo "==> Step Functions"
awsr stepfunctions delete-state-machine --state-machine-arn "$SM_ARN"
echo "==> Lambda functions and log groups"
for s in ingest extract-doc plan-audio transcribe-chunk analyze-image assemble; do
  awsr lambda delete-function --function-name "${PREFIX}-$s"
  awsr logs delete-log-group --log-group-name "/aws/lambda/${PREFIX}-$s"
done
echo "==> ECR"
awsr ecr delete-repository --repository-name "${PREFIX}-worker" --force
echo "==> S3 (empties the bucket first)"
if $DRY; then
  printf '  [plan] aws --profile %s --region %s s3 rm s3://%s --recursive\n' "$PROFILE" "$REGION" "$BUCKET"
else
  "${AWSCLI[@]}" s3 rm "s3://$BUCKET" --recursive >/dev/null 2>&1 && echo "  ok: emptied $BUCKET" || echo "  (bucket empty or missing)"
fi
awsr s3api delete-bucket --bucket "$BUCKET"
echo "==> IAM user qs-backend-app"
if ! $DRY; then
  for kid in $("${AWSCLI[@]}" iam list-access-keys --user-name qs-backend-app --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null); do
    awsr iam delete-access-key --user-name qs-backend-app --access-key-id "$kid"
  done
else
  echo "  [plan] aws iam delete-access-key for every key of qs-backend-app"
fi
awsr iam delete-user-policy --user-name qs-backend-app --policy-name qs-backend-app-inline
awsr iam delete-user --user-name qs-backend-app
echo "==> IAM roles"
awsr iam delete-role-policy --role-name "${PREFIX}-lambda-role" --policy-name qs-eval-lambda-inline
awsr iam delete-role --role-name "${PREFIX}-lambda-role"
awsr iam delete-role-policy --role-name "${PREFIX}-sfn-role" --policy-name qs-eval-sfn-inline
awsr iam delete-role --role-name "${PREFIX}-sfn-role"
echo "==> Budget"
awsr budgets delete-budget --account-id "$ACCOUNT_ID" --budget-name "${PREFIX}-monthly"

echo "==> infra/.env"
if $DRY; then
  echo "  [plan] remove S3_BUCKET, SFN_STATE_MACHINE_ARN, AWS_APP_ACCESS_KEY_ID, AWS_APP_SECRET_ACCESS_KEY from $ENV_FILE"
elif [[ -f "$ENV_FILE" ]]; then
  tmp="$(mktemp)"; grep -v -E '^(S3_BUCKET|SFN_STATE_MACHINE_ARN|AWS_APP_ACCESS_KEY_ID|AWS_APP_SECRET_ACCESS_KEY)=' "$ENV_FILE" > "$tmp" || true
  mv "$tmp" "$ENV_FILE"; echo "  removed the qs-eval variables"
fi
echo; echo "Done."
