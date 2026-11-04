<#
.SYNOPSIS
Day-2 (D-186, D-188) evidence for one environment, read-only: the drift timeline from DynamoDB, the repair state
machine runs, and the live S3 / CloudFront / HTTPS state. Changes nothing in AWS.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File scripts\day2_report.ps1 -EnvId env-c9fc390e
#>
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^env-[0-9a-f]{8}$')][string]$EnvId,
    [string]$AwsProfile = "sscp",  # not $Profile: an automatic variable
    [string]$Region = "ap-northeast-2",
    [string]$Table = "sscp-environments",
    [string]$RepairMachine = "sscp-repair"
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Invoke-Aws([string[]]$Arguments) {  # not "Aws": function names are case-insensitive and would shadow aws.exe
    $out = & aws.exe --profile $AwsProfile --region $Region @Arguments --output json
    if ($LASTEXITCODE -ne 0) { throw "aws $($Arguments -join ' ') failed" }
    ($out -join "`n") | ConvertFrom-Json
}
function Field($item, [string]$name) {  # a Store JSON_FIELDS attribute: a JSON string, or NULL
    $attr = $item.$name
    if ($null -eq $attr -or $attr.NULL) { return $null }
    if ($attr.S) { try { return $attr.S | ConvertFrom-Json } catch { return $attr.S } }
    return $attr
}
function Kst($when) {  # an ISO string, or a DateTime (ConvertFrom-Json and the AWS CLI may hand back either)
    if (-not $when) { return "-" }
    $t = if ($when -is [datetime]) { $when } else {
        [datetime]::Parse([string]$when, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)
    }
    ($t.ToUniversalTime().AddHours(9)).ToString("yyyy-MM-dd HH:mm:ss") + " KST"
}
function Secs($n) {
    if ($null -eq $n) { return "-" }
    if ($n -lt 120) { return "${n}s" }
    "{0}m{1:D2}s" -f [math]::Floor($n / 60), ($n % 60)
}

# Windows PowerShell 5.1 strips the double quotes from JSON passed to a native exe (7.3+ does not): use a file.
$keyFile = [IO.Path]::GetTempFileName()
[IO.File]::WriteAllText($keyFile, '{"env_id":{"S":"' + $EnvId + '"}}')
try { $item = (Invoke-Aws @("dynamodb", "get-item", "--table-name", $Table, "--key", "file://$keyFile")).Item }
finally { Remove-Item $keyFile -ErrorAction SilentlyContinue }
if (-not $item) { throw "$EnvId not found in $Table" }
$outputs = Field $item "outputs"

Write-Host "== $EnvId  status=$($item.status.S)" -ForegroundColor Cyan

Write-Host "`n-- Drift history (oldest first)" -ForegroundColor Yellow
$history = @(Field $item "drift_history" | ForEach-Object { $_ })  # 5.1 ConvertFrom-Json emits an array as one object
foreach ($d in $history) {
    "{0}  trigger={1,-8} event={2}  actor={3}  detect={4}  blocked={5}" -f (Kst $d.detected_at), $d.trigger,
        ($(if ($d.event_name) { $d.event_name } else { "-" })), ($(if ($d.actor) { $d.actor } else { "-" })),
        (Secs $d.detect_seconds), [bool]$d.endpoint_disabled
    "    failed: " + (($d.failed | ForEach-Object { $_.check_id }) -join ", ")
}
if ($history.Count -gt 1) {
    Write-Host "    note: $($history.Count) records. One console change should give one record (D-188)." -ForegroundColor DarkYellow
}

$drift = Field $item "drift"
if ($drift) {
    Write-Host "`n-- Open drift: detected $(Kst $drift.detected_at), event $($drift.event_name)" -ForegroundColor Yellow
}
$plan = Field $item "repair_plan"
if ($plan) {
    "-- Repair plan $(Kst $plan.created_at): passed=$($plan.passed) changes=$($plan.changes)"
    $plan.stacks | ForEach-Object { $_.changes } | ForEach-Object { "    $($_.address) ($($_.actions -join '/'))" }
}

$repair = Field $item "last_repair"
Write-Host "`n-- Last repair" -ForegroundColor Yellow
if ($repair) {
    "detected   $(Kst $repair.detected_at)   (detect $(Secs $repair.detect_seconds))"
    "approved   $(Kst $repair.approved_at)   by $($repair.approved_by)   (wait $(Secs $repair.wait_seconds))"
    "repaired   $(Kst $repair.repaired_at)   (apply $(Secs $repair.apply_seconds), total $(Secs $repair.repair_seconds))"
    if ($null -eq $repair.apply_seconds) {
        Write-Host "    note: no wait/apply split; recorded before the D-188 worker." -ForegroundColor DarkYellow
    }
} else { "-" }

$recheck = Field $item "last_recheck"
if ($recheck) { "`n-- Last recheck $(Kst $recheck.at) trigger=$($recheck.trigger) failed=[$($recheck.failed -join ', ')]" }

Write-Host "`n-- Repair state machine runs" -ForegroundColor Yellow
$arn = (Invoke-Aws @("stepfunctions", "list-state-machines")).stateMachines | Where-Object { $_.name -eq $RepairMachine } |
    Select-Object -ExpandProperty stateMachineArn
$runs = @((Invoke-Aws @("stepfunctions", "list-executions", "--state-machine-arn", $arn)).executions |
    Where-Object { $_.name -like "$EnvId-repair-*" } | Sort-Object startDate)
foreach ($r in $runs) {
    $took = if ($r.stopDate) { Secs ([int]([datetime]$r.stopDate - [datetime]$r.startDate).TotalSeconds) } else { "running" }
    "{0,-42} {1,-10} {2}  took {3}" -f $r.name, $r.status, (Kst $r.startDate), $took
}
$plans = @($runs | Where-Object { $_.name -like "*-repair-plan-*" }).Count
"plan runs: $plans, apply runs: $(@($runs | Where-Object { $_.name -like '*-repair-apply-*' }).Count)"

Write-Host "`n-- Live state" -ForegroundColor Yellow
if ($outputs.bucket_name) {
    $bpa = (Invoke-Aws @("s3api", "get-public-access-block", "--bucket", $outputs.bucket_name)).PublicAccessBlockConfiguration
    $off = @($bpa.PSObject.Properties | Where-Object { -not $_.Value } | ForEach-Object { $_.Name })
    "S3 BPA      " + $(if ($off.Count) { "OFF: $($off -join ', ')" } else { "all 4 on" })
}
if ($outputs.distribution_id) {
    $dist = (Invoke-Aws @("cloudfront", "get-distribution", "--id", $outputs.distribution_id)).Distribution
    "CloudFront  enabled=$($dist.DistributionConfig.Enabled) status=$($dist.Status)"
}
if ($outputs.url) {
    try { $code = (Invoke-WebRequest -Uri $outputs.url -UseBasicParsing -TimeoutSec 15).StatusCode }
    catch { $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { "no response" } }
    "HTTPS       $($outputs.url) -> $code"
}
