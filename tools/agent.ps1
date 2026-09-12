<#
.SYNOPSIS
    Install, remove and inspect the ClipForge agent — the thing that starts and
    stops the worker on request, so a phone can do more than queue work.

.DESCRIPTION
    ClipForge has no server-side executor. A job stays QUEUED until a process on
    this machine claims it, and until now the only things that could start that
    process were a terminal and the desktop app — both of which need someone
    standing here. The agent closes that gap: it starts with Windows, watches one
    Firestore document, and starts or stops the worker when the PWA writes to it.

    It is deliberately small. No service wrapper, no tray icon, no second daemon
    framework: one Scheduled Task at logon running `pythonw.exe -m clipforge
    agent`, which is a Python process holding an outbound listener and nothing
    else. Nothing on this machine listens for the internet.

    Registered as a logon task rather than a Windows service on purpose. A
    service runs as SYSTEM, in a different session, with a different HOME and no
    access to the user-scoped things the worker needs — the uv install, the
    service-account key, the GPU as the logged-in user sees it. Every one of
    those would have to be re-provisioned for SYSTEM to make a service work, and
    the only thing it would buy is running before login, which a machine that
    logs in automatically does not need.

.PARAMETER Install
    Register the logon task and start it now. Safe to re-run: it replaces the
    task rather than adding a second one.

.PARAMETER Uninstall
    Remove the task. Does not stop a worker the agent started — that is what the
    Stop button is for.

.PARAMETER Start
    Run the registered task now, without waiting for a logon.

.PARAMETER Stop
    Stop the agent. The worker it started keeps running: the agent supervises the
    worker, it does not own it, and killing a render because the supervisor was
    restarted would be the worse outcome.

.PARAMETER Status
    What is installed, what is running, and the tail of the agent's own log.

.PARAMETER Live
    Watch the real Firebase project rather than the local Emulator Suite. This is
    what makes the agent useful — `.env` deliberately pins the emulator so
    routine development cannot touch the real project, and an agent watching an
    emulator cannot be reached from a phone.

.EXAMPLE
    powershell -File tools/agent.ps1 -Install -Live   # start with Windows
    powershell -File tools/agent.ps1 -Status
    powershell -File tools/agent.ps1 -Live            # run here, in this window
#>
[CmdletBinding()]
param(
    [switch] $Install,
    [switch] $Uninstall,
    [switch] $Start,
    [switch] $Stop,
    [switch] $Status,
    [switch] $Live
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$TaskName = 'ClipForge agent'
$PythonW  = Join-Path $RepoRoot 'apps\worker\.venv\Scripts\pythonw.exe'
$LogFile  = Join-Path $env:USERPROFILE '.clipforge\agent.log'

function Fail($message) { Write-Host "`n   x $message" -ForegroundColor Red; exit 1 }
function Say($label, $value) { Write-Host ("  {0,-10}{1}" -f $label, $value) }

function Get-EnvValue([string] $Name) {
    $envPath = Join-Path $RepoRoot '.env'
    if (-not (Test-Path $envPath)) { return '' }
    foreach ($line in Get-Content $envPath) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }
        if ($trimmed.Substring(0, $split).Trim() -eq $Name) {
            return $trimmed.Substring($split + 1).Trim()
        }
    }
    return ''
}

function Get-AgentTask {
    # -ErrorAction SilentlyContinue would still make this tool report a failure,
    # so the absence of the task is caught rather than merely quietened.
    try { Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { $null }
}

Write-Host ''
Write-Host '  ClipForge agent' -ForegroundColor White
Write-Host '  ---------------'

# ─────────────────────────────────────────────────────────────────────────────
# Status
# ─────────────────────────────────────────────────────────────────────────────
if ($Status) {
    $task = Get-AgentTask
    if ($null -eq $task) {
        Say 'installed' 'no'
        Write-Host ''
        Write-Host '  Install it with: powershell -File tools/agent.ps1 -Install -Live'
        Write-Host ''
        exit 0
    }

    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Say 'installed' 'yes'
    Say 'state'     $task.State
    Say 'last run'  $info.LastRunTime
    Say 'last code' $info.LastTaskResult

    # The command line is what says which mode it was installed in, and a task
    # pointed at the emulator is the single most likely reason an agent looks
    # healthy here and invisible from a phone.
    $arguments = ($task.Actions | Select-Object -First 1).Arguments
    Say 'arguments' $arguments

    $running = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -like '*clipforge*agent*' }
    if ($running) {
        Say 'process' ("pid {0}" -f ($running | Select-Object -First 1).ProcessId)
    }
    else {
        Say 'process' 'not running'
    }

    if (Test-Path $LogFile) {
        Write-Host ''
        Write-Host "  last lines of $LogFile" -ForegroundColor DarkGray
        Get-Content $LogFile -Tail 12 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    }
    Write-Host ''
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Uninstall / start / stop
# ─────────────────────────────────────────────────────────────────────────────
if ($Uninstall) {
    if ($null -eq (Get-AgentTask)) { Fail "No '$TaskName' task is registered." }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host '  removed. A worker it started is still running; stop that from the app.'
    Write-Host ''
    exit 0
}

if ($Start) {
    if ($null -eq (Get-AgentTask)) { Fail "No '$TaskName' task is registered. Run -Install first." }
    Start-ScheduledTask -TaskName $TaskName
    Write-Host '  started.'
    Write-Host ''
    exit 0
}

if ($Stop) {
    if ($null -eq (Get-AgentTask)) { Fail "No '$TaskName' task is registered." }
    Stop-ScheduledTask -TaskName $TaskName
    Write-Host '  stopped. A worker it started keeps running.'
    Write-Host ''
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# Install
# ─────────────────────────────────────────────────────────────────────────────
if ($Install) {
    if (-not (Test-Path $PythonW)) {
        Fail @"
No worker environment at $PythonW

Create it first:
  uv sync --directory apps/worker
"@
    }

    $arguments = '-m clipforge agent --log-file "{0}"' -f $LogFile
    if ($Live) { $arguments = '-m clipforge agent --live --log-file "{0}"' -f $LogFile }

    if ($Live) {
        $projectId = Get-EnvValue 'CLIPFORGE_FIREBASE_PROJECT_ID'
        $credentials = Get-EnvValue 'CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS'
        if ([string]::IsNullOrWhiteSpace($projectId)) {
            Fail 'CLIPFORGE_FIREBASE_PROJECT_ID is not set in .env.'
        }
        if ([string]::IsNullOrWhiteSpace($credentials) -or -not (Test-Path $credentials)) {
            Fail @"
CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS does not point at a service account key.

The agent reads and writes Firestore as the worker does, so it needs the same
key. Generate one under Project settings -> Service accounts, point that
variable at it, and keep the file out of version control.
"@
        }
        Say 'target' "$projectId (LIVE)"
    }
    else {
        Say 'target' 'the local Emulator Suite'
        Write-Host '  note      an agent watching the emulator cannot be reached from a phone.' -ForegroundColor Yellow
        Write-Host '            pass -Live to install the one that can.' -ForegroundColor Yellow
    }

    $action = New-ScheduledTaskAction -Execute $PythonW -Argument $arguments -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

    # Every one of these settings is here because its default is wrong for a
    # process that is supposed to stay up:
    #   ExecutionTimeLimit 0  - the default kills the task after three days.
    #   AllowStartIfOnBatteries / DontStopIfGoingOnBatteries - the defaults stop
    #     it on a laptop, which is a surprising way to discover why the queue
    #     stalled.
    #   RestartCount/Interval - if the agent dies, bring it back. It is the thing
    #     that brings everything else back.
    #   MultipleInstances IgnoreNew - one supervisor. Two would race to start a
    #     worker, and the loser would report a foreign one.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Starts and stops the ClipForge worker on request, so it can be controlled from the PWA.' `
        -Force | Out-Null

    Start-ScheduledTask -TaskName $TaskName

    Say 'installed' $TaskName
    Say 'runs'      "$PythonW $arguments"
    Say 'log'       $LogFile
    Write-Host ''
    Write-Host '  It is running now and will start again at every logon.'
    Write-Host '  The Worker page in the app can now start and stop the worker from anywhere.'
    Write-Host ''
    exit 0
}

# ─────────────────────────────────────────────────────────────────────────────
# No switch: run it here, in this window, which is how you debug it.
# ─────────────────────────────────────────────────────────────────────────────
if ($Live) {
    $env:CLIPFORGE_USE_EMULATORS = 'false'
    Say 'target' ((Get-EnvValue 'CLIPFORGE_FIREBASE_PROJECT_ID') + ' (LIVE)')
}
else {
    $env:CLIPFORGE_USE_EMULATORS = 'true'
    Say 'target' 'the local Emulator Suite'
    Say 'hint' 'pass -Live to watch the real project'
}
Write-Host ''

Set-Location $RepoRoot
& uv run --directory apps/worker clipforge-worker agent
exit $LASTEXITCODE
