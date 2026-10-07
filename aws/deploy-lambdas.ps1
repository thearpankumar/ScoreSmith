<#
.SYNOPSIS
  Rebuild the worker image, push it to ECR, point the six existing qs-or functions at it and (re)apply their
  environment (OPENROUTER_* settings) so config changes in aws/config.ps1 reach already-deployed functions.
  Other infrastructure is NOT touched; run aws/bootstrap.ps1 for that (it also stores the OpenRouter key in SSM).
.DESCRIPTION
  .\aws\deploy-lambdas.ps1 -WhatIf   print the planned commands only (no AWS calls)
#>
[CmdletBinding()]
param([Alias('DryRun')][switch]$WhatIf, [Alias('Profile')][string]$AwsProfile = '')
$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$Here/config.ps1"
$Dry = [bool]$WhatIf
$AwsBase = @('--profile', $Profile_, '--region', $Region)
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("$Prefix-deploy-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Work | Out-Null
function FileUrl($p) { 'file://' + ($p -replace '\\', '/') }

function Invoke-AwsMut {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
  if ($Dry) { Write-Host "  [plan] aws $($AwsBase -join ' ') $($A -join ' ')"; return }
  & aws @AwsBase @A | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "aws $($A[0]) $($A[1]) failed (exit $LASTEXITCODE)" }
}
function Wait-Updated([string]$Fn) {
  if ($Dry) { Write-Host "  [plan] aws lambda wait function-updated-v2 --function-name $Fn"; return }
  & aws @AwsBase lambda wait function-updated-v2 --function-name $Fn
  if ($LASTEXITCODE -ne 0) { throw "lambda wait failed for $Fn" }
}

try {
  if ($Dry) { $AccountId = '<ACCOUNT_ID>' } else {
    $AccountId = (& aws @AwsBase sts get-caller-identity --query Account --output text).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'sts get-caller-identity failed' }
  }
  Write-Host "stack=$Prefix profile=$Profile_ account=$AccountId region=$Region"
  $Registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
  $Tag = 'v' + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
  $ImageUri = "$Registry/${Repo}:$Tag"
  $EnvJson = FileUrl (Write-LambdaEnvJson $Work)

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

  foreach ($s in $FunctionShorts) {
    $fn = "$Prefix-$s"
    Invoke-AwsMut lambda update-function-code --function-name $fn --image-uri $ImageUri
    Wait-Updated $fn
    Invoke-AwsMut lambda update-function-configuration --function-name $fn --environment $EnvJson
    Wait-Updated $fn
  }
  Write-Host "Deployed $ImageUri to $($FunctionShorts.Count) functions (code + environment)."
} finally {
  Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
