<#
.SYNOPSIS
  Idempotent (describe-or-create) provisioning of the qs-eval AI evaluation pipeline.

.DESCRIPTION
  .\aws\bootstrap.ps1 -WhatIf          print the planned commands only; makes NO AWS call and changes nothing
  .\aws\bootstrap.ps1                  create / update everything

  Everything uses `aws --profile arpan-aws --region us-east-1`. Secrets are written to git-ignored infra/.env and
  never printed.

.PARAMETER WhatIf
  Print planned commands only (no AWS calls at all, not even read-only ones). Alias: -DryRun.
.PARAMETER CorsOrigin
  Extra browser origin for S3 CORS (http://localhost:3000 is always allowed).
.PARAMETER Email
  Budget alert address.
.PARAMETER SkipImage
  Reuse the newest image already in ECR instead of building/pushing.
.PARAMETER RotateKey
  Delete the existing qs-backend-app access key(s) and create a new one.
#>
[CmdletBinding()]
param(
  [Alias('DryRun')][switch]$WhatIf,
  [string]$CorsOrigin = '',
  [string]$Email = 'arpankumar1119@gmail.com',
  [switch]$SkipImage,
  [switch]$RotateKey
)
$ErrorActionPreference = 'Stop'

$Profile_ = 'arpan-aws'
$Region = 'us-east-1'
$Prefix = 'qs-eval'
$StateMachine = 'qs-eval-pipeline'
$Repo = 'qs-eval-worker'
$LambdaRole = 'qs-eval-lambda-role'
$SfnRole = 'qs-eval-sfn-role'
$AppUser = 'qs-backend-app'
$BudgetName = 'qs-eval-monthly'
$Dry = [bool]$WhatIf

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $Here
$EnvFile = Join-Path $RepoRoot 'infra/.env'
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("qs-eval-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Work | Out-Null
$Utf8 = New-Object System.Text.UTF8Encoding $false

function Say($m) { Write-Host ""; Write-Host "==> $m" }
function Plan($m) { Write-Host "  [plan] $m" }
function FileUrl($p) { 'file://' + ($p -replace '\\', '/') }

$AwsBase = @('--profile', $Profile_, '--region', $Region)

# Mutating AWS CLI call. In dry-run it is only printed.
function Invoke-AwsMut {
  param([Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
  if ($Dry) { Plan ("aws " + ($AwsBase -join ' ') + " " + ($A -join ' ')); return }
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
# Read-only query returning text; dry-run returns the fallback without calling AWS.
function Get-AwsText {
  param([string]$Fallback, [Parameter(ValueFromRemainingArguments = $true)][string[]]$A)
  if ($Dry) { Plan "(read) aws $($A -join ' ')"; return $Fallback }
  $out = & aws @AwsBase @A 2>$null
  if ($LASTEXITCODE -ne 0) { return $Fallback }
  return ($out -join "`n").Trim()
}

# ------------------------------------------------------------------ account + names
if ($Dry) {
  $AccountId = '<ACCOUNT_ID>'
  Write-Host 'DRY RUN: nothing below is executed. No AWS call (not even read-only) is made.'
} else {
  if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'aws CLI not found' }
  $AccountId = (& aws @AwsBase sts get-caller-identity --query Account --output text).Trim()
  if ($LASTEXITCODE -ne 0) { throw 'sts get-caller-identity failed' }
}
$Bucket = "$Prefix-$AccountId-$Region"
$Registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$RepoUri = "$Registry/$Repo"
$LambdaRoleArn = "arn:aws:iam::${AccountId}:role/$LambdaRole"
$SfnRoleArn = "arn:aws:iam::${AccountId}:role/$SfnRole"
$SmArn = "arn:aws:states:${Region}:${AccountId}:stateMachine:$StateMachine"
function FnArn($short) { "arn:aws:lambda:${Region}:${AccountId}:function:$Prefix-$short" }
Write-Host "account=$AccountId region=$Region bucket=$Bucket"

# Render a template with the account placeholders; returns the path of the rendered copy.
function Render([string]$Src, [hashtable]$Extra = @{}) {
  $text = [System.IO.File]::ReadAllText($Src)
  $text = $text.Replace('${ACCOUNT_ID}', $AccountId).Replace('${REGION}', $Region).Replace('${BUCKET}', $Bucket).Replace('${BUDGET_EMAIL}', $Email)
  foreach ($k in $Extra.Keys) { $text = $text.Replace($k, $Extra[$k]) }
  $dst = Join-Path $Work (Split-Path -Leaf $Src)
  [System.IO.File]::WriteAllText($dst, $text, $Utf8)
  return $dst
}

try {
  # ---------------------------------------------------------------- 1. S3
  Say "S3 bucket $Bucket"
  if (Test-Aws s3api head-bucket --bucket $Bucket) {
    Write-Host '  bucket exists'
  } elseif ($Region -eq 'us-east-1') {
    Invoke-AwsMut s3api create-bucket --bucket $Bucket
  } else {
    Invoke-AwsMut s3api create-bucket --bucket $Bucket --create-bucket-configuration "LocationConstraint=$Region"
  }
  Invoke-AwsMut s3api put-public-access-block --bucket $Bucket --public-access-block-configuration 'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true'
  Invoke-AwsMut s3api put-bucket-ownership-controls --bucket $Bucket --ownership-controls 'Rules=[{ObjectOwnership=BucketOwnerEnforced}]'
  Invoke-AwsMut s3api put-bucket-encryption --bucket $Bucket --server-side-encryption-configuration (FileUrl (Render "$Here/policies/bucket-encryption.json"))
  Invoke-AwsMut s3api put-bucket-policy --bucket $Bucket --policy (FileUrl (Render "$Here/policies/bucket-policy.json"))
  $extra = @{}
  if ($CorsOrigin) { $extra['"http://localhost:3000"'] = "`"http://localhost:3000`", `"$CorsOrigin`"" }
  Invoke-AwsMut s3api put-bucket-cors --bucket $Bucket --cors-configuration (FileUrl (Render "$Here/policies/bucket-cors.json" $extra))
  Invoke-AwsMut s3api put-bucket-lifecycle-configuration --bucket $Bucket --lifecycle-configuration (FileUrl (Render "$Here/policies/bucket-lifecycle.json"))

  # ---------------------------------------------------------------- 2. ECR + image
  Say "ECR repository $Repo"
  if (Test-Aws ecr describe-repositories --repository-names $Repo) {
    Write-Host '  repository exists'
  } else {
    Invoke-AwsMut ecr create-repository --repository-name $Repo --image-scanning-configuration scanOnPush=true --encryption-configuration encryptionType=AES256
  }
  Invoke-AwsMut ecr put-lifecycle-policy --repository-name $Repo --lifecycle-policy-text (FileUrl (Render "$Here/policies/ecr-lifecycle.json"))
  Invoke-AwsMut ecr set-repository-policy --repository-name $Repo --policy-text (FileUrl (Render "$Here/policies/ecr-repo-policy.json"))

  $Tag = 'v' + (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
  $ImageUri = "${RepoUri}:$Tag"
  if ($SkipImage) {
    Say 'Reusing the newest image in ECR (-SkipImage)'
    $ImageUri = "${RepoUri}:latest"
  } else {
    Say "Build and push image $ImageUri (linux/amd64, no provenance attestation so Lambda accepts it)"
    if ($Dry) {
      Plan "aws $($AwsBase -join ' ') ecr get-login-password | docker login --username AWS --password-stdin $Registry"
      Plan "docker build --platform linux/amd64 --provenance=false -t $ImageUri -t ${RepoUri}:latest $Here/lambdas"
      Plan "docker push $ImageUri"
      Plan "docker push ${RepoUri}:latest"
    } else {
      & aws @AwsBase ecr get-login-password | docker login --username AWS --password-stdin $Registry
      if ($LASTEXITCODE -ne 0) { throw 'docker login to ECR failed' }
      docker build --platform linux/amd64 --provenance=false -t $ImageUri -t "${RepoUri}:latest" "$Here/lambdas"
      if ($LASTEXITCODE -ne 0) { throw 'docker build failed' }
      docker push $ImageUri
      if ($LASTEXITCODE -ne 0) { throw 'docker push failed' }
      docker push "${RepoUri}:latest"
      if ($LASTEXITCODE -ne 0) { throw 'docker push (latest) failed' }
    }
  }

  # ---------------------------------------------------------------- 3. IAM roles
  function Ensure-Role([string]$Name, [string]$Trust, [string]$PolicyName, [string]$PolicyFile, [string]$Desc) {
    $created = $false
    if (Test-Aws iam get-role --role-name $Name) {
      Write-Host "  role $Name exists (refreshing trust + inline policy)"
      Invoke-AwsMut iam update-assume-role-policy --role-name $Name --policy-document (FileUrl $Trust)
    } else {
      Invoke-AwsMut iam create-role --role-name $Name --assume-role-policy-document (FileUrl $Trust) --description $Desc
      $created = $true
    }
    Invoke-AwsMut iam put-role-policy --role-name $Name --policy-name $PolicyName --policy-document (FileUrl $PolicyFile)
    return $created
  }
  Say "IAM role $LambdaRole"
  $c1 = Ensure-Role $LambdaRole (Render "$Here/policies/lambda-trust.json") 'qs-eval-lambda-inline' (Render "$Here/policies/lambda-role-policy.json") 'qs-eval Lambda execution role (S3 prefixes, Bedrock models, logs)'
  Say "IAM role $SfnRole"
  $c2 = Ensure-Role $SfnRole (Render "$Here/policies/sfn-trust.json") 'qs-eval-sfn-inline' (Render "$Here/policies/sfn-role-policy.json") 'qs-eval Step Functions role (invoke the six functions only)'
  if ($c1 -or $c2) {
    Say 'Waiting 15s for new IAM roles to propagate'
    if ($Dry) { Plan 'Start-Sleep 15' } else { Start-Sleep 15 }
  }

  # ---------------------------------------------------------------- 4. Lambda functions
  # name | handler | memory MB | ephemeral MB | reserved concurrency
  $Functions = @(
    @('ingest', 'worker.handlers.ingest', 3008, 10240, 3),
    @('extract-doc', 'worker.handlers.extract_doc', 3008, 2048, 6),
    @('plan-audio', 'worker.handlers.plan_audio', 3008, 10240, 6),
    @('transcribe-chunk', 'worker.handlers.transcribe_chunk', 3008, 512, 12),
    @('analyze-image', 'worker.handlers.analyze_image', 3008, 512, 12),
    @('assemble', 'worker.handlers.assemble', 3008, 512, 3)
  )
  function Wait-Fn([string]$State, [string]$Fn) {
    if ($Dry) { Plan "aws lambda wait $State --function-name $Fn"; return }
    & aws @AwsBase lambda wait $State --function-name $Fn
    if ($LASTEXITCODE -ne 0) { throw "lambda wait $State failed for $Fn" }
  }

  Say 'Log groups (14-day retention) and Lambda functions'
  $TotalReserved = 0
  foreach ($f in $Functions) {
    $short, $handler, $mem, $eph, $reserved = $f
    $fn = "$Prefix-$short"
    $lg = "/aws/lambda/$fn"
    $TotalReserved += $reserved
    $found = Get-AwsText '' logs describe-log-groups --log-group-name-prefix $lg --query "length(logGroups[?logGroupName=='$lg'])" --output text
    if ($found -eq '1') { Write-Host "  log group $lg exists" } else { Invoke-AwsMut logs create-log-group --log-group-name $lg }
    Invoke-AwsMut logs put-retention-policy --log-group-name $lg --retention-in-days 14

    $imgCfgFile = Join-Path $Work "imgcfg-$short.json"
    [System.IO.File]::WriteAllText($imgCfgFile, "{`"Command`":[`"$handler`"]}", $Utf8)
    $imgCfg = FileUrl $imgCfgFile
    if (Test-Aws lambda get-function --function-name $fn) {
      Write-Host "  function $fn exists (updating configuration + code)"
      Invoke-AwsMut lambda update-function-configuration --function-name $fn --role $LambdaRoleArn --timeout 900 --memory-size $mem --ephemeral-storage "Size=$eph" --image-config $imgCfg
      Wait-Fn 'function-updated-v2' $fn
      Invoke-AwsMut lambda update-function-code --function-name $fn --image-uri $ImageUri
      Wait-Fn 'function-updated-v2' $fn
    } else {
      $created = $false
      for ($attempt = 1; $attempt -le 4 -and -not $created; $attempt++) {
        try {
          Invoke-AwsMut lambda create-function --function-name $fn --package-type Image --code "ImageUri=$ImageUri" --role $LambdaRoleArn --timeout 900 --memory-size $mem --ephemeral-storage "Size=$eph" --architectures x86_64 --image-config $imgCfg --environment 'Variables={LOG_LEVEL=INFO}' --description "qs-eval pipeline: $short"
          $created = $true
        } catch {
          Write-Host "  create-function failed (IAM propagation?); retrying in 10s ($attempt/4)"
          Start-Sleep 10
        }
      }
      if (-not $created) { throw "Could not create $fn" }
      Wait-Fn 'function-active-v2' $fn
    }
  }

  Say 'Reserved concurrency guard (only where the account allows; AWS keeps 100 unreserved)'
  $Unreserved = Get-AwsText '0' lambda get-account-settings --query 'AccountLimit.UnreservedConcurrentExecutions' --output text
  if ($Dry -or ($Unreserved -match '^\d+$' -and [int]$Unreserved -ge ($TotalReserved + 100))) {
    foreach ($f in $Functions) {
      Invoke-AwsMut lambda put-function-concurrency --function-name "$Prefix-$($f[0])" --reserved-concurrent-executions $f[4]
    }
  } else {
    Write-Host "  SKIPPED: unreserved concurrency is $Unreserved, need >= $($TotalReserved + 100). Request a Lambda concurrency quota increase, then re-run."
  }

  # ---------------------------------------------------------------- 5. State machine
  Say "Step Functions state machine $StateMachine"
  $asl = [System.IO.File]::ReadAllText("$Here/statemachine.asl.json")
  foreach ($pair in @(@('IngestFnArn', 'ingest'), @('ExtractDocFnArn', 'extract-doc'), @('PlanAudioFnArn', 'plan-audio'),
                      @('TranscribeChunkFnArn', 'transcribe-chunk'), @('AnalyzeImageFnArn', 'analyze-image'), @('AssembleFnArn', 'assemble'))) {
    $asl = $asl.Replace('${' + $pair[0] + '}', (FnArn $pair[1]))
  }
  $AslFile = Join-Path $Work 'statemachine.asl.json'
  [System.IO.File]::WriteAllText($AslFile, $asl, $Utf8)
  if (Test-Aws stepfunctions describe-state-machine --state-machine-arn $SmArn) {
    Invoke-AwsMut stepfunctions update-state-machine --state-machine-arn $SmArn --definition (FileUrl $AslFile) --role-arn $SfnRoleArn
  } else {
    Invoke-AwsMut stepfunctions create-state-machine --name $StateMachine --type STANDARD --definition (FileUrl $AslFile) --role-arn $SfnRoleArn
  }

  # ---------------------------------------------------------------- 6. Backend IAM user + key
  Say "IAM user $AppUser"
  if (Test-Aws iam get-user --user-name $AppUser) { Write-Host '  user exists' } else { Invoke-AwsMut iam create-user --user-name $AppUser }
  Invoke-AwsMut iam put-user-policy --user-name $AppUser --policy-name 'qs-backend-app-inline' --policy-document (FileUrl (Render "$Here/policies/backend-user-policy.json"))

  function Set-EnvVar([string]$Key, [string]$Value) {
    $lines = @()
    if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile | Where-Object { $_ -notmatch "^$([regex]::Escape($Key))=" }) }
    $lines += "$Key=$Value"
    [System.IO.File]::WriteAllLines($EnvFile, [string[]]$lines, $Utf8)
  }
  function Test-EnvHas([string]$Key) {
    (Test-Path $EnvFile) -and [bool](Select-String -Path $EnvFile -Pattern "^$([regex]::Escape($Key))=." -Quiet)
  }

  Say "Access key for $AppUser and infra/.env"
  if ($Dry) {
    Plan "(check) aws iam list-access-keys --user-name $AppUser"
    Plan "if the user has NO access key: aws iam create-access-key --user-name $AppUser  (output captured, not printed)"
    Plan "write S3_BUCKET, SFN_STATE_MACHINE_ARN (always) and AWS_APP_ACCESS_KEY_ID / AWS_APP_SECRET_ACCESS_KEY (only if a key was created) to $EnvFile"
    Plan 'with -RotateKey: aws iam delete-access-key for existing keys first'
  } else {
    git -C $RepoRoot check-ignore -q infra/.env
    if ($LASTEXITCODE -ne 0) { throw 'infra/.env is not git-ignored; refusing to write secrets' }
    if (-not (Test-Path $EnvFile)) { New-Item -ItemType File -Path $EnvFile | Out-Null }
    Set-EnvVar 'S3_BUCKET' $Bucket
    Set-EnvVar 'SFN_STATE_MACHINE_ARN' $SmArn
    if ($RotateKey) {
      $ids = (& aws @AwsBase iam list-access-keys --user-name $AppUser --query 'AccessKeyMetadata[].AccessKeyId' --output text) -split '\s+' | Where-Object { $_ }
      foreach ($kid in $ids) { & aws @AwsBase iam delete-access-key --user-name $AppUser --access-key-id $kid }
    }
    $nkeys = (& aws @AwsBase iam list-access-keys --user-name $AppUser --query 'length(AccessKeyMetadata)' --output text).Trim()
    if ($nkeys -eq '0') {
      $creds = (& aws @AwsBase iam create-access-key --user-name $AppUser --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text) -split '\s+'
      Set-EnvVar 'AWS_APP_ACCESS_KEY_ID' $creds[0]
      Set-EnvVar 'AWS_APP_SECRET_ACCESS_KEY' $creds[1]
      $creds = $null
      Write-Host '  created a new access key and stored it in infra/.env (not printed)'
    } elseif (Test-EnvHas 'AWS_APP_SECRET_ACCESS_KEY') {
      Write-Host '  access key already exists and infra/.env has credentials; left unchanged'
    } else {
      Write-Host "  WARNING: $AppUser already has an access key but infra/.env has no secret (it cannot be retrieved)."
      Write-Host '           Re-run with -RotateKey to replace it.'
    }
  }

  # ---------------------------------------------------------------- 7. Budget
  Say "AWS Budget $BudgetName (`$25/month, alerts to $Email)"
  if (Test-Aws budgets describe-budget --account-id $AccountId --budget-name $BudgetName) {
    Write-Host '  budget exists'
  } else {
    Invoke-AwsMut budgets create-budget --account-id $AccountId --budget (FileUrl (Render "$Here/policies/budget.json")) --notifications-with-subscribers (FileUrl (Render "$Here/policies/budget-notifications.json"))
  }

  Say 'Done'
  if ($Dry) {
    Write-Host 'Dry run finished: no AWS call was made.'
  } else {
    Write-Host "Bucket:        $Bucket"
    Write-Host "State machine: $SmArn"
    Write-Host 'Credentials and ids are in infra/.env. Update code later with aws/deploy-lambdas.ps1.'
  }
} finally {
  Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
