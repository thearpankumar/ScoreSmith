<#
.SYNOPSIS
  Idempotent (describe-or-create) provisioning of the OpenRouter variant of the AI evaluation pipeline
  (separate "qs-or" stack; never touches the main-branch Bedrock deployment).

.DESCRIPTION
  .\aws\bootstrap.ps1 -WhatIf          print the planned commands only; makes NO AWS call and changes nothing
  .\aws\bootstrap.ps1                  create / update everything

  Everything uses `aws --profile arpan-aws --region us-east-1`. Names come from aws/config.ps1.
  The OpenRouter API key is read from $env:OPENROUTER_API_KEY, then from OPENROUTER_API_KEY= in infra/.env, and is
  stored in SSM Parameter Store (SecureString); it is never printed and never placed in Lambda env vars.
  Generated credentials / ids are written to the git-ignored env file (OR_* variables only) and never printed.

.PARAMETER WhatIf
  Print planned commands only (no AWS calls at all, not even read-only ones). Alias: -DryRun.
.PARAMETER CorsOrigin
  Extra browser origin for S3 CORS (http://localhost:3000 is always allowed).
.PARAMETER Email
  Budget alert address.
.PARAMETER SkipImage
  Reuse the newest image already in ECR instead of building/pushing.
.PARAMETER RotateKey
  Delete the existing qs-or-backend-app access key(s) and create a new one.
.PARAMETER EnvFile
  Env file to read OPENROUTER_API_KEY from and to write the OR_* variables to (default infra/.env).
.PARAMETER AwsProfile
  AWS CLI profile (default arpan-aws).
#>
[CmdletBinding()]
param(
  [Alias('DryRun')][switch]$WhatIf,
  [string]$CorsOrigin = '',
  [string]$Email = 'arpankumar1119@gmail.com',
  [switch]$SkipImage,
  [switch]$RotateKey,
  [string]$EnvFile = '',
  [Alias('Profile')][string]$AwsProfile = ''
)
$ErrorActionPreference = 'Stop'

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
. "$Here/config.ps1"
$Dry = [bool]$WhatIf

$RepoRoot = Split-Path -Parent $Here
if (-not $EnvFile) { $EnvFile = Join-Path $RepoRoot 'infra/.env' }
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("$Prefix-" + [guid]::NewGuid().ToString('N'))
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
Write-Host "stack=$Prefix profile=$Profile_ account=$AccountId region=$Region bucket=$Bucket"

# Render a template with the placeholders; returns the path of the rendered copy.
function Render([string]$Src, [hashtable]$Extra = @{}) {
  $text = [System.IO.File]::ReadAllText($Src)
  $map = [ordered]@{
    '${ACCOUNT_ID}'    = $AccountId
    '${REGION}'        = $Region
    '${BUCKET}'        = $Bucket
    '${BUDGET_EMAIL}'  = $Email
    '${PREFIX}'        = $Prefix
    '${APP_USER}'      = $AppUser
    '${STATE_MACHINE}' = $StateMachine
    '${BUDGET_NAME}'   = $BudgetName
    '${SSM_PARAM}'     = $SsmParam
  }
  foreach ($k in $map.Keys) { $text = $text.Replace($k, $map[$k]) }
  foreach ($k in $Extra.Keys) { $text = $text.Replace($k, $Extra[$k]) }
  $dst = Join-Path $Work (Split-Path -Leaf $Src)
  [System.IO.File]::WriteAllText($dst, $text, $Utf8)
  return $dst
}

# ------------------------------------------------------------------ env file helpers (never print values)
function Set-EnvVar([string]$Key, [string]$Value) {
  $lines = @()
  if (Test-Path $EnvFile) { $lines = @(Get-Content $EnvFile | Where-Object { $_ -notmatch "^$([regex]::Escape($Key))=" }) }
  $lines += "$Key=$Value"
  [System.IO.File]::WriteAllText($EnvFile, (($lines -join "`n") + "`n"), $Utf8)
}
function Test-EnvHas([string]$Key) {
  (Test-Path $EnvFile) -and [bool](Select-String -Path $EnvFile -Pattern "^$([regex]::Escape($Key))=." -Quiet)
}
# OPENROUTER_API_KEY: process env first, then the env file. Returns @{ Value; Source } or $null.
function Get-OpenRouterKey {
  if ($env:OPENROUTER_API_KEY -and $env:OPENROUTER_API_KEY.Trim()) {
    return @{ Value = $env:OPENROUTER_API_KEY.Trim(); Source = 'the OPENROUTER_API_KEY environment variable' }
  }
  if (Test-Path $EnvFile) {
    foreach ($line in (Get-Content $EnvFile)) {
      if ($line -match '^\s*(?:export\s+)?OPENROUTER_API_KEY\s*=\s*(.*?)\s*$') {
        $v = $Matches[1]
        if ($v -match '^"([^"]*)"' -or $v -match "^'([^']*)'" -or $v -match '^(\S*)') { $v = $Matches[1] }
        if ($v) { return @{ Value = $v; Source = $EnvFile } }
      }
    }
  }
  return $null
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

  # ---------------------------------------------------------------- 3. OpenRouter key -> SSM SecureString
  Say "OpenRouter API key -> SSM SecureString $SsmParam"
  $KeyStored = $false
  $keyInfo = Get-OpenRouterKey
  if ($keyInfo) {
    $keyFile = Join-Path $Work 'openrouter-key.txt'
    [System.IO.File]::WriteAllText($keyFile, $keyInfo.Value, $Utf8)   # no trailing newline; deleted with $Work
    Write-Host "  key found in $($keyInfo.Source) (value not printed)"
    Invoke-AwsMut ssm put-parameter --name $SsmParam --type SecureString --overwrite --value (FileUrl $keyFile) --description "OpenRouter API key for the $Prefix pipeline Lambdas"
    $KeyStored = $true
    $keyInfo = $null
  } elseif (Test-Aws ssm get-parameter --name $SsmParam) {
    Write-Host "  WARNING: no OPENROUTER_API_KEY in the environment or in $EnvFile; keeping the key already stored in SSM."
  } else {
    Write-Host "  WARNING: no OPENROUTER_API_KEY found (environment variable or $EnvFile) and $SsmParam does not exist yet."
    Write-Host '           The Lambdas are still deployed but audio/image analysis will fail until the key is stored.'
    Write-Host "           Add  OPENROUTER_API_KEY=...  to $EnvFile and re-run .\aws\bootstrap.ps1 (idempotent)."
  }

  # ---------------------------------------------------------------- 4. IAM roles
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
  $c1 = Ensure-Role $LambdaRole (Render "$Here/policies/lambda-trust.json") $LambdaPolicyName (Render "$Here/policies/lambda-role-policy.json") "$Prefix Lambda execution role (S3 prefixes, one SSM parameter, logs)"
  Say "IAM role $SfnRole"
  $c2 = Ensure-Role $SfnRole (Render "$Here/policies/sfn-trust.json") $SfnPolicyName (Render "$Here/policies/sfn-role-policy.json") "$Prefix Step Functions role (invoke the six functions only)"
  if ($c1 -or $c2) {
    Say 'Waiting 15s for new IAM roles to propagate'
    if ($Dry) { Plan 'Start-Sleep 15' } else { Start-Sleep 15 }
  }

  # ---------------------------------------------------------------- 5. Lambda functions
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

  $EnvJson = FileUrl (Write-LambdaEnvJson $Work)
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
      Write-Host "  function $fn exists (updating configuration + environment + code)"
      Invoke-AwsMut lambda update-function-configuration --function-name $fn --role $LambdaRoleArn --timeout 900 --memory-size $mem --ephemeral-storage "Size=$eph" --image-config $imgCfg --environment $EnvJson
      Wait-Fn 'function-updated-v2' $fn
      Invoke-AwsMut lambda update-function-code --function-name $fn --image-uri $ImageUri
      Wait-Fn 'function-updated-v2' $fn
    } else {
      $created = $false
      for ($attempt = 1; $attempt -le 4 -and -not $created; $attempt++) {
        try {
          Invoke-AwsMut lambda create-function --function-name $fn --package-type Image --code "ImageUri=$ImageUri" --role $LambdaRoleArn --timeout 900 --memory-size $mem --ephemeral-storage "Size=$eph" --architectures x86_64 --image-config $imgCfg --environment $EnvJson --description "$Prefix pipeline: $short"
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

  # Account-level guard. AWS always keeps 100 unreserved executions, and a new account's limit is only 10, so
  # reservations are applied only where the account can afford them. The unreserved figure already excludes what
  # THIS stack reserved on an earlier run, so that amount is added back (keeps re-runs idempotent). A second
  # deployment in the same account (e.g. the main-branch stack + qs-or) needs another 42 on top of the first stack's: when the
  # account cannot afford it the step is skipped with a message and everything else still works.
  Say 'Reserved concurrency guard (only where the account allows; AWS keeps 100 unreserved)'
  $Unreserved = Get-AwsText '0' lambda get-account-settings --query 'AccountLimit.UnreservedConcurrentExecutions' --output text
  $OwnReserved = 0
  foreach ($f in $Functions) {
    $cur = Get-AwsText '0' lambda get-function-concurrency --function-name "$Prefix-$($f[0])" --query ReservedConcurrentExecutions --output text
    if ($cur -match '^\d+$') { $OwnReserved += [int]$cur }
  }
  if ($Dry -or ($Unreserved -match '^\d+$' -and ([int]$Unreserved + $OwnReserved) -ge ($TotalReserved + 100))) {
    foreach ($f in $Functions) {
      Invoke-AwsMut lambda put-function-concurrency --function-name "$Prefix-$($f[0])" --reserved-concurrent-executions $f[4]
    }
  } else {
    Write-Host "  SKIPPED: unreserved concurrency is $Unreserved (+$OwnReserved already reserved by $Prefix), need >= $($TotalReserved + 100). Request a Lambda concurrency quota increase, then re-run. Everything else works without reservations."
  }

  # ---------------------------------------------------------------- 6. State machine
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

  # ---------------------------------------------------------------- 7. Backend IAM user + key
  Say "IAM user $AppUser"
  if (Test-Aws iam get-user --user-name $AppUser) { Write-Host '  user exists' } else { Invoke-AwsMut iam create-user --user-name $AppUser }
  Invoke-AwsMut iam put-user-policy --user-name $AppUser --policy-name $AppUserPolicyName --policy-document (FileUrl (Render "$Here/policies/backend-user-policy.json"))

  Say "Access key for $AppUser and $EnvFile"
  if ($Dry) {
    Plan "(check) aws iam list-access-keys --user-name $AppUser"
    Plan "if the user has NO access key: aws iam create-access-key --user-name $AppUser  (output captured, not printed)"
    Plan "write $($EnvKeys[0]), $($EnvKeys[1]) (always) and $($EnvKeys[2]) / $($EnvKeys[3]) (only if a key was created) to $EnvFile; no other variable is touched"
    Plan 'with -RotateKey: aws iam delete-access-key for existing keys first'
  } else {
    git -C $RepoRoot check-ignore -q $EnvFile
    if ($LASTEXITCODE -eq 1) { throw "$EnvFile is not git-ignored; refusing to write secrets" }
    if (-not (Test-Path $EnvFile)) { New-Item -ItemType File -Path $EnvFile | Out-Null }
    Set-EnvVar 'OR_S3_BUCKET' $Bucket
    Set-EnvVar 'OR_SFN_STATE_MACHINE_ARN' $SmArn
    if ($RotateKey) {
      $ids = (& aws @AwsBase iam list-access-keys --user-name $AppUser --query 'AccessKeyMetadata[].AccessKeyId' --output text) -split '\s+' | Where-Object { $_ }
      foreach ($kid in $ids) { & aws @AwsBase iam delete-access-key --user-name $AppUser --access-key-id $kid }
    }
    $nkeys = (& aws @AwsBase iam list-access-keys --user-name $AppUser --query 'length(AccessKeyMetadata)' --output text).Trim()
    if ($nkeys -eq '0') {
      $creds = (& aws @AwsBase iam create-access-key --user-name $AppUser --query 'AccessKey.[AccessKeyId,SecretAccessKey]' --output text) -split '\s+'
      Set-EnvVar 'OR_AWS_APP_ACCESS_KEY_ID' $creds[0]
      Set-EnvVar 'OR_AWS_APP_SECRET_ACCESS_KEY' $creds[1]
      $creds = $null
      Write-Host "  created a new access key and stored it in $EnvFile (not printed)"
    } elseif (Test-EnvHas 'OR_AWS_APP_SECRET_ACCESS_KEY') {
      Write-Host "  access key already exists and $EnvFile has credentials; left unchanged"
    } else {
      Write-Host "  WARNING: $AppUser already has an access key but $EnvFile has no secret (it cannot be retrieved)."
      Write-Host '           Re-run with -RotateKey to replace it.'
    }
  }

  # ---------------------------------------------------------------- 8. Budget
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
    Write-Host "SSM parameter: $SsmParam (key stored this run: $KeyStored)"
    Write-Host "OR_* credentials and ids are in $EnvFile. Update code later with aws/deploy-lambdas.ps1."
    if (-not $KeyStored) { Write-Host "NOTE: add OPENROUTER_API_KEY to $EnvFile and re-run this script to store/refresh the key in SSM." }
  }
} finally {
  Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
