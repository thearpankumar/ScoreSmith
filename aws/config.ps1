# Single source of truth for every resource name of the OpenRouter variant ("qs-or" stack).
# Dot-sourced by bootstrap.ps1, deploy-lambdas.ps1 and teardown.ps1 so the names cannot drift apart.
# config.sh is the bash twin: keep the two in sync.
#
# The "qs-or" prefix is deliberately different from the Bedrock deployment on the main branch:
# nothing here can address, update or delete a main-branch resource.

$Profile_ = if ($AwsProfile) { $AwsProfile } else { 'arpan-aws' }
$Region = 'us-east-1'
$Prefix = 'qs-or'
if ($Prefix -notmatch '^qs-or(-[a-z0-9]+)*$') { throw "Refusing to run: prefix '$Prefix' must stay inside the qs-or namespace" }

$StateMachine = "$Prefix-pipeline"
$Repo = "$Prefix-worker"
$LambdaRole = "$Prefix-lambda-role"
$SfnRole = "$Prefix-sfn-role"
$AppUser = "$Prefix-backend-app"
$BudgetName = "$Prefix-monthly"
$LambdaPolicyName = "$Prefix-lambda-inline"
$SfnPolicyName = "$Prefix-sfn-inline"
$AppUserPolicyName = "$Prefix-backend-app-inline"
$SsmParam = "/$Prefix/openrouter-api-key"          # SecureString (default aws/ssm key; no CMK, so no kms statement)
$FunctionShorts = @('ingest', 'extract-doc', 'plan-audio', 'transcribe-chunk', 'analyze-image', 'assemble')

# Variable names written to / removed from infra/.env (new names; the Bedrock stack's S3_BUCKET etc. are never touched)
$EnvKeys = @('OR_S3_BUCKET', 'OR_SFN_STATE_MACHINE_ARN', 'OR_AWS_APP_ACCESS_KEY_ID', 'OR_AWS_APP_SECRET_ACCESS_KEY')

# Lambda environment. Process env vars of the same name override the defaults. NO secret goes here: the API key
# lives in SSM and the function only gets its parameter name.
$OrBaseUrl = if ($env:OPENROUTER_BASE_URL) { $env:OPENROUTER_BASE_URL } else { 'https://openrouter.ai/api/v1' }
$OrTranscribeModels = if ($env:OPENROUTER_TRANSCRIBE_MODELS) { $env:OPENROUTER_TRANSCRIBE_MODELS } else { 'mistralai/voxtral-small-24b-2507,google/gemini-2.5-flash' }
$OrVisionModels = if ($env:OPENROUTER_VISION_MODELS) { $env:OPENROUTER_VISION_MODELS } else { 'openai/gpt-6-luna,deepseek/deepseek-v4.1-flash,google/gemini-2.5-flash' }

# Writes the --environment JSON file (commas in model lists make the CLI shorthand unusable) and returns its path.
function Write-LambdaEnvJson([string]$Dir) {
  $vars = [ordered]@{
    OPENROUTER_SECRET_PARAM      = $SsmParam
    OPENROUTER_BASE_URL          = $OrBaseUrl
    OPENROUTER_TRANSCRIBE_MODELS = $OrTranscribeModels
    OPENROUTER_VISION_MODELS     = $OrVisionModels
    LOG_LEVEL                    = 'INFO'
  }
  $json = (@{ Variables = $vars } | ConvertTo-Json -Depth 4 -Compress)
  $path = Join-Path $Dir 'lambda-env.json'
  [System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding $false))
  return $path
}
