<#
.SYNOPSIS
  Remove EVERYTHING that aws/bootstrap.ps1 created for the qs-or stack (bucket contents included, irreversibly).
  The main-branch Bedrock deployment is never touched.
.DESCRIPTION
  .\aws\teardown.ps1 -WhatIf   print the planned commands only (no AWS calls)
  .\aws\teardown.ps1           asks you to type the bucket name to confirm
  .\aws\teardown.ps1 -Yes      skip the prompt
  -EnvFile PATH                env file to remove the OR_* variables from (default infra/.env)
#>
[CmdletBinding()]
param([Alias('DryRun')][switch]$WhatIf, [switch]$Yes, [string]$EnvFile = '', [Alias('Profile')][string]$AwsProfile = '')
$ErrorActionPreference = 'Continue'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$Here/config.ps1"
$Dry = [bool]$WhatIf
if (-not $EnvFile) { $EnvFile = Join-Path (Split-Path -Parent $Here) 'infra/.env' }
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
$SmArn = "arn:aws:states:${Region}:${AccountId}:stateMachine:$StateMachine"

Write-Host "This will PERMANENTLY delete the '$Prefix' stack in account $AccountId ($Region):"
Write-Host "  S3 bucket $Bucket and ALL its objects, ECR repo $Repo, $($FunctionShorts.Count) Lambda functions + log groups,"
Write-Host "  state machine $StateMachine, IAM roles $LambdaRole / $SfnRole, IAM user $AppUser (+keys),"
Write-Host "  SSM parameter $SsmParam, budget $BudgetName, and the OR_* lines in $EnvFile."
if (-not $Dry -and -not $Yes) {
  $answer = Read-Host "Type the bucket name ($Bucket) to continue"
  if ($answer -ne $Bucket) { Write-Host 'Aborted.'; exit 1 }
}

Write-Host "`n==> Step Functions"
Invoke-AwsMut stepfunctions delete-state-machine --state-machine-arn $SmArn
Write-Host '==> Lambda functions and log groups'
foreach ($s in $FunctionShorts) {
  Invoke-AwsMut lambda delete-function --function-name "$Prefix-$s"
  Invoke-AwsMut logs delete-log-group --log-group-name "/aws/lambda/$Prefix-$s"
}
Write-Host '==> SSM parameter (OpenRouter key)'
Invoke-AwsMut ssm delete-parameter --name $SsmParam
Write-Host '==> ECR'
Invoke-AwsMut ecr delete-repository --repository-name $Repo --force
Write-Host '==> S3 (empties the bucket first)'
Invoke-AwsMut s3 rm "s3://$Bucket" --recursive
Invoke-AwsMut s3api delete-bucket --bucket $Bucket
Write-Host "==> IAM user $AppUser"
if ($Dry) { Write-Host "  [plan] aws iam delete-access-key for every key of $AppUser" } else {
  $ids = (& aws @AwsBase iam list-access-keys --user-name $AppUser --query 'AccessKeyMetadata[].AccessKeyId' --output text 2>$null) -split '\s+' | Where-Object { $_ }
  foreach ($kid in $ids) { Invoke-AwsMut iam delete-access-key --user-name $AppUser --access-key-id $kid }
}
Invoke-AwsMut iam delete-user-policy --user-name $AppUser --policy-name $AppUserPolicyName
Invoke-AwsMut iam delete-user --user-name $AppUser
Write-Host '==> IAM roles'
Invoke-AwsMut iam delete-role-policy --role-name $LambdaRole --policy-name $LambdaPolicyName
Invoke-AwsMut iam delete-role --role-name $LambdaRole
Invoke-AwsMut iam delete-role-policy --role-name $SfnRole --policy-name $SfnPolicyName
Invoke-AwsMut iam delete-role --role-name $SfnRole
Write-Host '==> Budget'
Invoke-AwsMut budgets delete-budget --account-id $AccountId --budget-name $BudgetName

Write-Host "==> $EnvFile"
if ($Dry) {
  Write-Host "  [plan] remove $($EnvKeys -join ', ') from $EnvFile (all other variables, incl. OPENROUTER_API_KEY, stay)"
} elseif (Test-Path $EnvFile) {
  $pattern = '^(' + (($EnvKeys | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')='
  $keep = @(Get-Content $EnvFile | Where-Object { $_ -notmatch $pattern })
  $text = if ($keep.Count) { ($keep -join "`n") + "`n" } else { '' }
  [System.IO.File]::WriteAllText($EnvFile, $text, (New-Object System.Text.UTF8Encoding $false))
  Write-Host "  removed the $Prefix variables"
}
Write-Host "`nDone."
