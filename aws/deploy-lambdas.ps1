<#
.SYNOPSIS
  Rebuild the worker image, push it to ECR and point the six existing functions at it. Before each code update the
  function environment (LOG_LEVEL, OPENROUTER_SECRET_PARAM) is re-applied, and a non-empty OPENROUTER_API_KEY in
  infra/.env is copied to the SSM SecureString the Lambdas read. Other infrastructure is NOT touched; run
  aws/bootstrap.ps1 for that.
.DESCRIPTION
  .\aws\deploy-lambdas.ps1 -WhatIf   print the planned commands only (no AWS calls)
#>
[CmdletBinding()]
param([Alias('DryRun')][switch]$WhatIf)
$ErrorActionPreference = 'Stop'
$Profile_ = 'arpan-aws'; $Region = 'us-east-1'; $Prefix = 'qs-eval'; $Repo = 'qs-eval-worker'
$SsmParam = "/$Prefix/openrouter-api-key"
$Dry = [bool]$WhatIf
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path (Split-Path -Parent $Here) 'infra/.env'
$AwsBase = @('--profile', $Profile_, '--region', $Region)
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("qs-eval-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Work | Out-Null
$Utf8 = New-Object System.Text.UTF8Encoding $false

function Plan($m) { Write-Host "  [plan] $m" }
function FileUrl($p) { 'file://' + ($p -replace '\\', '/') }
function Invoke-AwsMut {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
  if ($Dry) { Plan "aws $($AwsBase -join ' ') $($A -join ' ')"; return }
  & aws @AwsBase @A | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "aws $($A[0]) $($A[1]) failed (exit $LASTEXITCODE)" }
}
# Read-only existence check. In dry-run nothing is called and it reports 'missing'.
function Test-Aws {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
  if ($Dry) { Plan "(check) aws $($A -join ' ')"; return $false }
  & aws @AwsBase @A *> $null
  return ($LASTEXITCODE -eq 0)
}

# Value of $Key in infra/.env; the last occurrence wins (as in docker compose), surrounding quotes and a trailing
# ' # comment' are dropped. Empty when absent. Callers must never print it. (Same as in bootstrap.ps1.)
function Get-EnvFileValue([string]$Key) {
  if (-not (Test-Path $EnvFile)) { return '' }
  $line = Get-Content $EnvFile | Where-Object { $_ -match "^\s*(export\s+)?$([regex]::Escape($Key))\s*=" } | Select-Object -Last 1
  if (-not $line) { return '' }
  $v = ($line -replace '^[^=]*=', '').Trim()
  if ($v -match '^"([^"]*)"' -or $v -match "^'([^']*)'" -or $v -match '^(\S*)') { return $Matches[1] }
  return ''
}

# OPENROUTER_API_KEY from infra/.env -> SSM SecureString, through a private temp file (never on a command line).
function Sync-OpenRouterKey {
  $key = Get-EnvFileValue 'OPENROUTER_API_KEY'
  if ($key) {
    if ($Dry) {
      Plan "aws $($AwsBase -join ' ') ssm put-parameter --name $SsmParam --type SecureString --overwrite --value file://<temp file with OPENROUTER_API_KEY from $EnvFile>"
    } else {
      $keyFile = Join-Path $Work 'openrouter-key.txt'
      [System.IO.File]::WriteAllText($keyFile, $key, $Utf8)
      try {
        & aws @AwsBase ssm put-parameter --name $SsmParam --type SecureString --overwrite --value (FileUrl $keyFile) --description "OpenRouter API key for the $Prefix pipeline Lambdas" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "aws ssm put-parameter failed (exit $LASTEXITCODE)" }
      } finally {
        Remove-Item -Force $keyFile -ErrorAction SilentlyContinue
      }
      Write-Host "  stored OPENROUTER_API_KEY from $EnvFile in $SsmParam (value not printed)"
    }
  } elseif (Test-Aws ssm get-parameter --name $SsmParam) {
    Write-Host "  no OPENROUTER_API_KEY in $EnvFile; keeping the key already stored in $SsmParam"
  } else {
    Write-Host "  NOTE: no OPENROUTER_API_KEY in $EnvFile and $SsmParam does not exist. Evaluations that use OpenRouter"
    Write-Host "        fail fast (at ingest) until it is set: add the key to $EnvFile and re-run this script."
  }
  $key = $null
}

try {
  if ($Dry) { $AccountId = '<ACCOUNT_ID>' } else {
    $AccountId = (& aws @AwsBase sts get-caller-identity --query Account --output text).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'sts get-caller-identity failed' }
  }
  $Registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
  $Tag = 'v' + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
  $ImageUri = "$Registry/${Repo}:$Tag"

  Write-Host "==> OpenRouter API key -> SSM SecureString $SsmParam"
  Sync-OpenRouterKey

  # The Lambda role's inline policy, re-applied so a stack bootstrapped before the OpenRouter change gets
  # ssm:GetParameter on the key parameter (same rendering as bootstrap.ps1).
  Write-Host "==> IAM inline policy $Prefix-lambda-inline on $Prefix-lambda-role"
  $RolePolicyFile = Join-Path $Work 'lambda-role-policy.json'
  $policy = (Get-Content -Raw (Join-Path $Here 'policies/lambda-role-policy.json')).Replace('${ACCOUNT_ID}', $AccountId).Replace('${REGION}', $Region).Replace('${BUCKET}', "$Prefix-$AccountId-$Region")
  [System.IO.File]::WriteAllText($RolePolicyFile, $policy, $Utf8)
  Invoke-AwsMut iam put-role-policy --role-name "$Prefix-lambda-role" --policy-name "$Prefix-lambda-inline" --policy-document (FileUrl $RolePolicyFile)

  Write-Host "==> Build and push $ImageUri"
  if ($Dry) {
    Plan "aws $($AwsBase -join ' ') ecr get-login-password | docker login --username AWS --password-stdin $Registry"
    Plan "docker build --platform linux/amd64 --provenance=false -t $ImageUri -t $Registry/${Repo}:latest $Here/lambdas"
    Plan "docker push $ImageUri"
    Plan "docker push $Registry/${Repo}:latest"
  } else {
    & aws @AwsBase ecr get-login-password | docker login --username AWS --password-stdin $Registry
    if ($LASTEXITCODE -ne 0) { throw 'docker login to ECR failed' }
    docker build --platform linux/amd64 --provenance=false -t $ImageUri -t "$Registry/${Repo}:latest" "$Here/lambdas"
    if ($LASTEXITCODE -ne 0) { throw 'docker build failed' }
    docker push $ImageUri
    if ($LASTEXITCODE -ne 0) { throw 'docker push failed' }
    docker push "$Registry/${Repo}:latest"
    if ($LASTEXITCODE -ne 0) { throw 'docker push (latest) failed' }
  }

  # The function environment (the SSM parameter name only, never the key), then the code.
  $LambdaEnvFile = Join-Path $Work 'lambda-env.json'
  [System.IO.File]::WriteAllText($LambdaEnvFile, (@{ Variables = [ordered]@{ LOG_LEVEL = 'INFO'; OPENROUTER_SECRET_PARAM = $SsmParam } } | ConvertTo-Json -Compress), $Utf8)
  $LambdaEnv = FileUrl $LambdaEnvFile
  function Wait-Updated([string]$Fn) {
    if ($Dry) { Plan "aws $($AwsBase -join ' ') lambda wait function-updated-v2 --function-name $Fn"; return }
    & aws @AwsBase lambda wait function-updated-v2 --function-name $Fn
    if ($LASTEXITCODE -ne 0) { throw "lambda wait failed for $Fn" }
  }
  Write-Host '==> Lambda functions (environment + code)'
  foreach ($s in 'ingest', 'extract-doc', 'plan-audio', 'transcribe-chunk', 'analyze-image', 'assemble') {
    Invoke-AwsMut lambda update-function-configuration --function-name "$Prefix-$s" --environment $LambdaEnv
    Wait-Updated "$Prefix-$s"
    Invoke-AwsMut lambda update-function-code --function-name "$Prefix-$s" --image-uri $ImageUri
    Wait-Updated "$Prefix-$s"
  }
  Write-Host "Deployed $ImageUri to 6 functions (environment + code)."
} finally {
  Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
