<#
.SYNOPSIS
  Rebuild the worker image, push it to ECR and point the six existing functions at it (update-function-code only).
  Infrastructure is NOT touched; run aws/bootstrap.ps1 for that.
.DESCRIPTION
  .\aws\deploy-lambdas.ps1 -WhatIf   print the planned commands only (no AWS calls)
#>
[CmdletBinding()]
param([Alias('DryRun')][switch]$WhatIf)
$ErrorActionPreference = 'Stop'
$Profile_ = 'arpan-aws'; $Region = 'us-east-1'; $Prefix = 'qs-eval'; $Repo = 'qs-eval-worker'
$Dry = [bool]$WhatIf
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$AwsBase = @('--profile', $Profile_, '--region', $Region)

function Invoke-AwsMut {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
  if ($Dry) { Write-Host "  [plan] aws $($AwsBase -join ' ') $($A -join ' ')"; return }
  & aws @AwsBase @A | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "aws $($A[0]) $($A[1]) failed (exit $LASTEXITCODE)" }
}

if ($Dry) { $AccountId = '<ACCOUNT_ID>' } else {
  $AccountId = (& aws @AwsBase sts get-caller-identity --query Account --output text).Trim()
  if ($LASTEXITCODE -ne 0) { throw 'sts get-caller-identity failed' }
}
$Registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$Tag = 'v' + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
$ImageUri = "$Registry/${Repo}:$Tag"

if ($Dry) {
  Write-Host "  [plan] aws $($AwsBase -join ' ') ecr get-login-password | docker login --username AWS --password-stdin $Registry"
  Write-Host "  [plan] docker build --platform linux/amd64 --provenance=false -t $ImageUri -t $Registry/${Repo}:latest $Here/lambdas"
  Write-Host "  [plan] docker push $ImageUri"
  Write-Host "  [plan] docker push $Registry/${Repo}:latest"
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

foreach ($s in 'ingest', 'extract-doc', 'plan-audio', 'transcribe-chunk', 'analyze-image', 'assemble') {
  Invoke-AwsMut lambda update-function-code --function-name "$Prefix-$s" --image-uri $ImageUri
  if ($Dry) { Write-Host "  [plan] aws lambda wait function-updated-v2 --function-name $Prefix-$s" } else {
    & aws @AwsBase lambda wait function-updated-v2 --function-name "$Prefix-$s"
    if ($LASTEXITCODE -ne 0) { throw "lambda wait failed for $Prefix-$s" }
  }
}
Write-Host "Deployed $ImageUri to 6 functions."
