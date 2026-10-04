<#
.SYNOPSIS
  Remove EVERYTHING that aws/bootstrap.ps1 created (bucket contents included, irreversibly).
.DESCRIPTION
  .\aws\teardown.ps1 -WhatIf   print the planned commands only (no AWS calls)
  .\aws\teardown.ps1           asks you to type the bucket name to confirm
  .\aws\teardown.ps1 -Yes      skip the prompt
#>
[CmdletBinding()]
param([Alias('DryRun')][switch]$WhatIf, [switch]$Yes)
$ErrorActionPreference = 'Continue'
$Profile_ = 'arpan-aws'; $Region = 'us-east-1'; $Prefix = 'qs-eval'
$Dry = [bool]$WhatIf
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path (Split-Path -Parent $Here) 'infra/.env'
$AwsBase = @('--profile', $Profile_, '--region', $Region)

function Invoke-AwsMut {  # failures (already gone) are reported but do not stop the teardown
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
  if ($Dry) { Write-Host "  [plan] aws $($AwsBase -join ' ') $($A -join ' ')"; return }
  & aws @AwsBase @A *> $null
  if ($LASTEXITCODE -eq 0) { Write-Host "  ok: aws $($A[0]) $($A[1])" } else { Write-Host "  (skipped, probably already gone): aws $($A[0]) $($A[1])" }
}

if ($Dry) { $AccountId = '<ACCOUNT_ID>' } else {
  $AccountId = (& aws @AwsBase sts get-caller-identity --query Account --output text).Trim()
  if ($LASTEXITCODE -ne 0) { throw 'sts get-caller-identity failed' }
}
$Bucket = "$Prefix-$AccountId-$Region"
$SmArn = "arn:aws:states:${Region}:${AccountId}:stateMachine:$Prefix-pipeline"

Write-Host "This will PERMANENTLY delete in account $AccountId ($Region):"
Write-Host "  S3 bucket $Bucket and ALL its objects, ECR repo $Prefix-worker, 6 Lambda functions + log groups,"
Write-Host "  state machine $Prefix-pipeline, IAM roles $Prefix-lambda-role / $Prefix-sfn-role, IAM user qs-backend-app (+keys),"
Write-Host "  budget $Prefix-monthly, and the related lines in infra/.env."
if (-not $Dry -and -not $Yes) {
  $answer = Read-Host "Type the bucket name ($Bucket) to continue"
  if ($answer -ne $Bucket) { Write-Host 'Aborted.'; exit 1 }
}

Write-Host "`n==> Step Functions"
Invoke-AwsMut stepfunctions delete-state-machine --state-machine-arn $SmArn
Write-Host '==> Lambda functions and log groups'
foreach ($s in 'ingest', 'extract-doc', 'plan-audio', 'transcribe-chunk', 'analyze-image', 'assemble') {
  Invoke-AwsMut lambda delete-function --function-name "$Prefix-$s"
  Invoke-AwsMut logs delete-log-group --log-group-name "/aws/lambda/$Prefix-$s"
}
Write-Host '==> ECR'
Invoke-AwsMut ecr delete-repository --repository-name "$Prefix-worker" --force
Write-Host '==> S3 (empties the bucket first)'
Invoke-AwsMut s3 rm "s3://$Bucket" --recursive
Invoke-AwsMut s3api delete-bucket --bucket $Bucket
Write-Host '==> IAM user qs-backend-app'
if ($Dry) { Write-Host '  [plan] aws iam delete-access-key for every key of qs-backend-app' } else {
  $ids = (& aws @AwsBase iam list-access-keys --user-name qs-backend-app --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>$null) -split '\s+' | Where-Object { $_ }
  foreach ($kid in $ids) { Invoke-AwsMut iam delete-access-key --user-name qs-backend-app --access-key-id $kid }
}
Invoke-AwsMut iam delete-user-policy --user-name qs-backend-app --policy-name qs-backend-app-inline
Invoke-AwsMut iam delete-user --user-name qs-backend-app
Write-Host '==> IAM roles'
Invoke-AwsMut iam delete-role-policy --role-name "$Prefix-lambda-role" --policy-name qs-eval-lambda-inline
Invoke-AwsMut iam delete-role --role-name "$Prefix-lambda-role"
Invoke-AwsMut iam delete-role-policy --role-name "$Prefix-sfn-role" --policy-name qs-eval-sfn-inline
Invoke-AwsMut iam delete-role --role-name "$Prefix-sfn-role"
Write-Host '==> Budget'
Invoke-AwsMut budgets delete-budget --account-id $AccountId --budget-name "$Prefix-monthly"

Write-Host '==> infra/.env'
if ($Dry) {
  Write-Host "  [plan] remove S3_BUCKET, SFN_STATE_MACHINE_ARN, AWS_APP_ACCESS_KEY_ID, AWS_APP_SECRET_ACCESS_KEY from $EnvFile"
} elseif (Test-Path $EnvFile) {
  $keep = @(Get-Content $EnvFile | Where-Object { $_ -notmatch '^(S3_BUCKET|SFN_STATE_MACHINE_ARN|AWS_APP_ACCESS_KEY_ID|AWS_APP_SECRET_ACCESS_KEY)=' })
  [System.IO.File]::WriteAllLines($EnvFile, [string[]]$keep, (New-Object System.Text.UTF8Encoding $false))
  Write-Host '  removed the qs-eval variables'
}
Write-Host "`nDone."
