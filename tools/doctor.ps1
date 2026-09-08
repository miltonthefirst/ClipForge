<#
.SYNOPSIS
    Verifies that this machine can build and run ClipForge.

.DESCRIPTION
    Phase 0 exit criterion. Every dependency ClipForge relies on is checked here,
    including the two that fail late and confusingly if left unverified:

      * CTranslate2 resolving the cuBLAS / cuDNN DLLs on Windows
      * ffmpeg exposing h264_nvenc and the libass / loudnorm / silencedetect filters

    Environment checks run in this script; anything needing the worker's virtual
    environment is delegated to `clipforge.diagnostics`, so there is exactly one
    implementation of each check.

.PARAMETER SkipGpu
    Skip GPU-dependent checks. Use on a machine with no NVIDIA card.

.PARAMETER Json
    Emit machine-readable JSON instead of the table.

.EXAMPLE
    pwsh -File tools/doctor.ps1
    pwsh -File tools/doctor.ps1 -SkipGpu
#>
[CmdletBinding()]
param(
    [switch] $SkipGpu,
    [switch] $Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot  = Split-Path -Parent $PSScriptRoot
$WorkerDir = Join-Path $RepoRoot 'apps/worker'
$WebDir    = Join-Path $RepoRoot 'apps/web'
$Results   = [System.Collections.Generic.List[object]]::new()

# winget installs land in per-user link directories that an already-open shell
# has not picked up. Rebuild PATH from the registry so a fresh install works
# without the user restarting their terminal.
$env:Path = @(
    (Join-Path $env:USERPROFILE '.local\bin'),
    [System.Environment]::GetEnvironmentVariable('Path', 'Machine'),
    [System.Environment]::GetEnvironmentVariable('Path', 'User')
) -join ';'

function Add-Result {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [bool]   $Ok,
        [string] $Detail = '',
        [string] $Fix    = ''
    )
    $Results.Add([pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail; Fix = $Fix })
}

function Test-Tool {
    param(
        [Parameter(Mandatory)] [string] $Name,
        [Parameter(Mandatory)] [string] $Command,
        [string[]] $VersionArgs = @('--version'),
        [string]   $Fix = '',
        [string]   $MinimumMajor = ''
    )

    $resolved = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $resolved) {
        Add-Result -Name $Name -Ok $false -Detail 'not found on PATH' -Fix $Fix
        return
    }

    try {
        $raw = (& $Command @VersionArgs 2>&1 | Select-Object -First 1) -join ' '
    } catch {
        Add-Result -Name $Name -Ok $false -Detail "found but not runnable: $_" -Fix $Fix
        return
    }

    $detail = $raw.Trim()
    if ($MinimumMajor -and $detail -match '(\d+)\.(\d+)') {
        if ([int]$Matches[1] -lt [int]$MinimumMajor) {
            Add-Result -Name $Name -Ok $false -Detail "$detail (need major >= $MinimumMajor)" -Fix $Fix
            return
        }
    }
    Add-Result -Name $Name -Ok $true -Detail $detail
}

# --- Core toolchain ----------------------------------------------------------
Test-Tool -Name 'git'      -Command 'git'      -Fix 'https://git-scm.com/download/win'
Test-Tool -Name 'node'     -Command 'node'     -MinimumMajor '20' -Fix 'winget install OpenJS.NodeJS.LTS'
Test-Tool -Name 'npm'      -Command 'npm'      -Fix 'ships with Node'
Test-Tool -Name 'uv'       -Command 'uv'       -Fix 'winget install astral-sh.uv'
Test-Tool -Name 'ffmpeg'   -Command 'ffmpeg'   -VersionArgs @('-version') -Fix 'winget install Gyan.FFmpeg'
Test-Tool -Name 'ffprobe'  -Command 'ffprobe'  -VersionArgs @('-version') -Fix 'winget install Gyan.FFmpeg'
Test-Tool -Name 'ollama'   -Command 'ollama'   -Fix 'https://ollama.com/download'
Test-Tool -Name 'firebase' -Command 'firebase' -Fix 'npm install -g firebase-tools'

# --- Pinned Python interpreter -----------------------------------------------
# The system Python is 3.14, for which PyTorch and CTranslate2 publish no wheels.
# The worker must resolve to the uv-managed 3.12 instead.
if (Get-Command uv -ErrorAction SilentlyContinue) {
    try {
        $pythonVersion = (& uv run --project $WorkerDir python --version 2>&1 | Select-Object -First 1).ToString().Trim()
        if ($pythonVersion -match '3\.12') {
            Add-Result -Name 'python (worker venv)' -Ok $true -Detail $pythonVersion
        } else {
            Add-Result -Name 'python (worker venv)' -Ok $false `
                -Detail "$pythonVersion - expected 3.12.x" `
                -Fix 'uv python install 3.12; uv sync --project apps/worker'
        }
    } catch {
        Add-Result -Name 'python (worker venv)' -Ok $false -Detail "uv run failed: $_" `
            -Fix 'uv sync --project apps/worker --extra gpu'
    }
}

# --- Disk headroom -----------------------------------------------------------
# Sources are large and the workspace GC needs room to work (docs/PLAN.md Phase 3).
$drive  = (Get-Item $RepoRoot).PSDrive
$freeGb = [math]::Round($drive.Free / 1GB, 1)
Add-Result -Name 'disk headroom' -Ok ($freeGb -ge 20) `
    -Detail "$freeGb GB free on $($drive.Name):" `
    -Fix 'free at least 20 GB, or point CLIPFORGE_WORKSPACE_DIR at a roomier drive'

# --- GPU / inference / media, via the worker environment ---------------------
if (Get-Command uv -ErrorAction SilentlyContinue) {
    $diagArgs = @('run', '--project', $WorkerDir, 'python', '-m', 'clipforge.diagnostics', '--json')
    if ($SkipGpu) { $diagArgs += '--skip-gpu' }

    $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
    $raw = (& uv @diagArgs 2>$null) -join "`n"

    if ([string]::IsNullOrWhiteSpace($raw)) {
        Add-Result -Name 'worker diagnostics' -Ok $false -Detail 'produced no output' `
            -Fix 'uv sync --project apps/worker --extra gpu'
    } else {
        try {
            foreach ($check in ($raw | ConvertFrom-Json)) {
                $fix = switch ($check.name) {
                    'ffmpeg'          { 'winget install Gyan.FFmpeg (needs the FULL build for libass)' }
                    'ollama'          { 'ollama serve; ollama pull qwen3.5:4b' }
                    'gpu'             { 'install/repair the NVIDIA driver' }
                    'cuda_libs'       { 'uv sync --project apps/worker --extra gpu' }
                    'cuda_transcribe' { 'uv sync --project apps/worker --extra gpu' }
                    default           { '' }
                }
                Add-Result -Name $check.name -Ok ([bool]$check.ok) -Detail $check.detail -Fix $fix
            }
        } catch {
            Add-Result -Name 'worker diagnostics' -Ok $false -Detail "unparseable output: $raw"
        }
    }
}

# --- Web app dependencies ----------------------------------------------------
if (Test-Path (Join-Path $WebDir 'package.json')) {
    $installed = Test-Path (Join-Path $WebDir 'node_modules')
    Add-Result -Name 'web node_modules' -Ok $installed `
        -Detail $(if ($installed) { 'installed' } else { 'missing' }) `
        -Fix 'npm ci --prefix apps/web'
}

# --- Report ------------------------------------------------------------------
if ($Json) {
    $Results | ConvertTo-Json -Depth 4
} else {
    Write-Host ''
    Write-Host '  ClipForge doctor' -ForegroundColor Cyan
    Write-Host '  ----------------' -ForegroundColor Cyan
    foreach ($r in $Results) {
        $tag   = if ($r.Ok) { 'PASS' } else { 'FAIL' }
        $color = if ($r.Ok) { 'Green' } else { 'Red' }
        Write-Host ('  [{0}] ' -f $tag) -ForegroundColor $color -NoNewline
        Write-Host ('{0,-22} {1}' -f $r.Name, $r.Detail)
        if (-not $r.Ok -and $r.Fix) {
            Write-Host ('         -> {0}' -f $r.Fix) -ForegroundColor Yellow
        }
    }

    $failed = @($Results | Where-Object { -not $_.Ok })
    Write-Host ''
    if ($failed.Count -eq 0) {
        Write-Host '  All checks passed. This machine can build and run ClipForge.' -ForegroundColor Green
    } else {
        Write-Host ("  {0} check(s) failed." -f $failed.Count) -ForegroundColor Red
    }
    Write-Host ''
}

exit @($Results | Where-Object { -not $_.Ok }).Count
