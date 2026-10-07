#!/usr/bin/env bash
# Single source of truth for every resource name of the OpenRouter variant ("qs-or" stack).
# Sourced by bootstrap.sh, deploy-lambdas.sh and teardown.sh so the names cannot drift apart.
# config.ps1 is the PowerShell twin: keep the two in sync.
#
# The "qs-or" prefix is deliberately different from the Bedrock deployment on the main branch:
# nothing here can address, update or delete a main-branch resource.

PROFILE="arpan-aws"   # bootstrap/deploy/teardown --profile NAME overrides after sourcing
REGION="us-east-1"
PREFIX="qs-or"
[[ "$PREFIX" =~ ^qs-or(-[a-z0-9]+)*$ ]] || { echo "Refusing to run: prefix '$PREFIX' must stay inside the qs-or namespace" >&2; exit 1; }

STATE_MACHINE="${PREFIX}-pipeline"
REPO="${PREFIX}-worker"
LAMBDA_ROLE="${PREFIX}-lambda-role"
SFN_ROLE="${PREFIX}-sfn-role"
APP_USER="${PREFIX}-backend-app"
BUDGET_NAME="${PREFIX}-monthly"
LAMBDA_POLICY_NAME="${PREFIX}-lambda-inline"
SFN_POLICY_NAME="${PREFIX}-sfn-inline"
APP_USER_POLICY_NAME="${PREFIX}-backend-app-inline"
SSM_PARAM="/${PREFIX}/openrouter-api-key"   # SecureString (default aws/ssm key; no CMK, so no kms statement)
FUNCTION_SHORTS=(ingest extract-doc plan-audio transcribe-chunk analyze-image assemble)

# Variable names written to / removed from infra/.env (new names; the Bedrock stack's S3_BUCKET etc. are never touched)
ENV_KEYS=(OR_S3_BUCKET OR_SFN_STATE_MACHINE_ARN OR_AWS_APP_ACCESS_KEY_ID OR_AWS_APP_SECRET_ACCESS_KEY)

# Lambda environment. Process env vars of the same name override the defaults. NO secret goes here: the API key
# lives in SSM and the function only gets its parameter name.
OR_BASE_URL="${OPENROUTER_BASE_URL:-https://openrouter.ai/api/v1}"
OR_TRANSCRIBE_MODELS="${OPENROUTER_TRANSCRIBE_MODELS:-mistralai/voxtral-small-24b-2507,google/gemini-2.5-flash}"
OR_VISION_MODELS="${OPENROUTER_VISION_MODELS:-openai/gpt-6-luna,deepseek/deepseek-v4.1-flash,google/gemini-2.5-flash}"

# write_lambda_env_json DIR -> prints the path of the --environment JSON file (commas in the model lists make the CLI
# shorthand unusable).
write_lambda_env_json() {
  local path="$1/lambda-env.json"
  printf '{"Variables":{"OPENROUTER_SECRET_PARAM":"%s","OPENROUTER_BASE_URL":"%s","OPENROUTER_TRANSCRIBE_MODELS":"%s","OPENROUTER_VISION_MODELS":"%s","LOG_LEVEL":"INFO"}}' \
    "$SSM_PARAM" "$OR_BASE_URL" "$OR_TRANSCRIBE_MODELS" "$OR_VISION_MODELS" > "$path"
  echo "$path"
}
