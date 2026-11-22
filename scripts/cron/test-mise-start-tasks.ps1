# Run on Windows PowerShell; mocks never call the real Task Scheduler.
param([Parameter(Mandatory=$true)][string]$Script,
      [ValidateSet('running','ready','ready-check','missing','disabled','immediate-exit')][string]$Case)
$global:fake = @{ MiseContinuous = 'Running'; MiseMedia = 'Running' }
if ($Case -eq 'ready') { $global:fake.MiseContinuous = 'Ready' }
if ($Case -eq 'ready-check') { $global:fake.MiseContinuous = 'Ready' }
if ($Case -eq 'missing') { $global:fake.Remove('MiseContinuous') }
if ($Case -eq 'disabled') { $global:fake.MiseContinuous = 'Disabled' }
if ($Case -eq 'immediate-exit') { $global:fake.MiseContinuous = 'Ready' }
function Get-ScheduledTask {
    param($TaskPath, $TaskName)
    if (-not $global:fake.ContainsKey($TaskName)) { throw 'not found' }
    [PSCustomObject]@{ State = $global:fake[$TaskName] }
}
function Start-ScheduledTask {
    param($TaskPath, $TaskName)
    if ($Case -eq 'ready-check') { throw 'check-only started task' }
    if ($Case -ne 'immediate-exit') { $global:fake[$TaskName] = 'Running' }
}
function Get-ScheduledTaskInfo { param($TaskPath, $TaskName) [PSCustomObject]@{ LastTaskResult = 1 } }
function Start-Sleep { param($Seconds) }
if ($Case -eq 'ready-check') { & $Script -CheckOnly } else { & $Script }
if ($LASTEXITCODE) { exit $LASTEXITCODE }
