# Start the existing collection and media scheduled tasks only when they are idle.
# This script never resets a checkout, removes a lock, or clears a stopped state.
# -CheckOnly reports what would happen without starting a task.
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$failed = $false
foreach ($name in @('MiseContinuous', 'MiseMedia')) {
    try {
        $task = Get-ScheduledTask -TaskPath '\' -TaskName $name
        if ($task.State -eq 'Running') {
            Write-Output "$name`: already running"
        } elseif ($task.State -eq 'Ready') {
            if ($CheckOnly) {
                Write-Output "$name`: ready (check only)"
            } else {
                Start-ScheduledTask -TaskPath '\' -TaskName $name
                Start-Sleep -Seconds 3
                $task = Get-ScheduledTask -TaskPath '\' -TaskName $name
                if ($task.State -ne 'Running') {
                    $info = Get-ScheduledTaskInfo -TaskPath '\' -TaskName $name
                    throw "did not remain running (state=$($task.State), result=$($info.LastTaskResult))"
                }
                Write-Output "$name`: started"
            }
        } else {
            throw "cannot start task in state $($task.State)"
        }
    } catch {
        Write-Output "$name`: ERROR: $($_.Exception.Message)"
        $failed = $true
    }
}
if ($failed) { exit 1 }
