#!/usr/bin/env bash
# Remove EVERYTHING that aws/bootstrap.sh created for the qs-or stack (bucket contents included, irreversibly).
# The main-branch Bedrock deployment is never touched.
#   ./aws/teardown.sh --dry-run    print the planned commands only (no AWS calls)
#   ./aws/teardown.sh              asks you to type the bucket name to confirm
#   ./aws/teardown.sh --yes        skip the prompt (scripts / CI)
#   --env-file PATH                env file to remove the OR_* variables from (default infra/.env)
#   --profile NAME                 AWS CLI profile (default arpan-aws)
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=config.sh
source "$HERE/config.sh"
ENV_FILE="$(cd "$HERE/.." && pwd)/infra/.env"
DRY=false; YES=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=true ;; --yes) YES=true ;;
    --env-file) ENV_FILE="${2:?--env-file needs a value}"; shift ;;
    --profile) PROFILE="${2:?--profile needs a value}"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

AWSCLI=(aws --profile "$PROFILE" --region "$REGION")
run() {  # mutating; failures (already gone) are reported but do not stop the teardown
  if $DRY; then printf '  [plan] '; printf '%q ' "$@"; printf '\n'; return 0; fi
  "$@" >/dev/null 2>&1 && echo "  ok: $*" || echo "  (skipped, probably already gone): $*"
}
awsr() { run "${AWSCLI[@]}" "$@"; }

if $DRY; then ACCOUNT_ID="<ACCOUNT_ID>"; else ACCOUNT_ID="$("${AWSCLI[@]}" sts get-caller-identity --query Account --output text)" || exit 1; fi
BUCKET="${PREFIX}-${ACCOUNT_ID}-${REGION}"
SM_ARN="arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${STATE_MACHINE}"

echo "This will PERMANENTLY delete the '$PREFIX' stack in account $ACCOUNT_ID ($REGION):"
echo "  S3 bucket $BUCKET and ALL its objects, ECR repo $REPO, ${#FUNCTION_SHORTS[@]} Lambda functions + log groups,"
echo "  state machine $STATE_MACHINE, IAM roles $LAMBDA_ROLE / $SFN_ROLE, IAM user $APP_USER (+keys),"
echo "  SSM parameter $SSM_PARAM, budget $BUDGET_NAME, and the OR_* lines in $ENV_FILE."
if ! $DRY && ! $YES; then
  read -r -p "Type the bucket name ($BUCKET) to continue: " answer
  [[ "$answer" == "$BUCKET" ]] || { echo "Aborted."; exit 1; }
fi

echo; echo "==> Step Functions"
awsr stepfunctions delete-state-machine --state-machine-arn "$SM_ARN"
echo "==> Lambda functions and log groups"
for s in "${FUNCTION_SHORTS[@]}"; do
  awsr lambda delete-function --function-name "${PREFIX}-$s"
  awsr logs delete-log-group --log-group-name "/aws/lambda/${PREFIX}-$s"
done
echo "==> SSM parameter (OpenRouter key)"
awsr ssm delete-parameter --name "$SSM_PARAM"
echo "==> ECR"
awsr ecr delete-repository --repository-name "$REPO" --force
echo "==> S3 (empties the bucket first)"
if $DRY; then
  printf '  [plan] aws --profile %s --region %s s3 rm s3://%s --recursive\n' "$PROFILE" "$REGION" "$BUCKET"
else
  "${AWSCLI[@]}" s3 rm "s3://$BUCKET" --recursive >/dev/null 2>&1 && echo "  ok: emptied $BUCKET" || echo "  (bucket empty or missing)"
fi
awsr s3api delete-bucket --bucket "$BUCKET"
echo "==> IAM user $APP_USER"
if ! $DRY; then
  for kid in $("${AWSCLI[@]}" iam list-access-keys --user-name "$APP_USER" --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>/dev/null); do
    awsr iam delete-access-key --user-name "$APP_USER" --access-key-id "$kid"
  done
else
  echo "  [plan] aws iam delete-access-key for every key of $APP_USER"
fi
awsr iam delete-user-policy --user-name "$APP_USER" --policy-name "$APP_USER_POLICY_NAME"
awsr iam delete-user --user-name "$APP_USER"
echo "==> IAM roles"
awsr iam delete-role-policy --role-name "$LAMBDA_ROLE" --policy-name "$LAMBDA_POLICY_NAME"
awsr iam delete-role --role-name "$LAMBDA_ROLE"
awsr iam delete-role-policy --role-name "$SFN_ROLE" --policy-name "$SFN_POLICY_NAME"
awsr iam delete-role --role-name "$SFN_ROLE"
echo "==> Budget"
awsr budgets delete-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME"

echo "==> $ENV_FILE"
if $DRY; then
  echo "  [plan] remove ${ENV_KEYS[*]} from $ENV_FILE (all other variables, incl. OPENROUTER_API_KEY, stay)"
elif [[ -f "$ENV_FILE" ]]; then
  tmp="$(mktemp)"
  pattern="^($(IFS='|'; echo "${ENV_KEYS[*]}"))="
  grep -v -E "$pattern" "$ENV_FILE" > "$tmp" || true
  mv "$tmp" "$ENV_FILE"; echo "  removed the $PREFIX variables"
fi
echo; echo "Done."
