<#
.SYNOPSIS
    Run the ClipForge worker, against the emulator by default or the real
    Firebase project on request.

.DESCRIPTION
    `.env` deliberately keeps CLIPFORGE_USE_EMULATORS=true, and a unit test
    asserts it — routine development must not touch, or cost anything on, the
    real project. That default is worth keeping even now that the project is
    deployed: `.env` is read by every worker command, so flipping it would point
    `submit`, `status` and anything else at production too.

    Going live is therefore an argument rather than a config edit. Real
    environment variables take precedence over `.env` in pydantic-settings, so
    -Live overrides the one setting that matters and leaves everything else
    exactly as the file says.

.PARAMETER Live
    Run against the real Firebase project named in .env, using the service
    account key it points at. Without this the worker talks to the local
    Emulator Suite and needs no credentials at all.

.PARAMETER Once
    Claim and run a single job, then exit. Useful for a smoke test.

.EXAMPLE
    pwsh -File tools/worker.ps1                # emulator
    pwsh -File tools/worker.ps1 -Live          # the real project
    pwsh -File tools/worker.ps1 -Live -Once    # one real job, then stop
#>
[CmdletBinding()]
param(
    [switch] $Live,
    [switch] $Once
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Fail($message) { Write-Host "`n   x $message" -ForegroundColor Red; exit 1 }

$envPath = Join-Path $repoRoot '.env'
if (-not (Test-Path $envPath)) {
    Fail ".env not found. Copy .env.example to .env first."
}

$config = @{}
foreach ($line in Get-Content $envPath) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }
    $config[$trimmed.Substring(0, $split).Trim()] = $trimmed.Substring($split + 1).Trim()
}

Write-Host ''
Write-Host '  ClipForge worker' -ForegroundColor White
Write-Host '  ----------------'

if ($Live) {
    $projectId = $config['CLIPFORGE_FIREBASE_PROJECT_ID']
    $credentials = $config['CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS']

    if ([string]::IsNullOrWhiteSpace($projectId)) {
        Fail 'CLIPFORGE_FIREBASE_PROJECT_ID is not set in .env.'
    }
    if ([string]::IsNullOrWhiteSpace($credentials)) {
        Fail @"
CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS is not set in .env.

Generate a key in the Firebase console under
  Project settings -> Service accounts -> Generate new private key
and point that variable at it. Keep the file out of version control.
"@
    }
    if (-not (Test-Path $credentials)) {
        Fail "No service account key at $credentials"
    }

    # Overriding the process environment, not the file: `.env` stays
    # emulator-pointed for every other command run from this repository.
    $env:CLIPFORGE_USE_EMULATORS = 'false'

    # The bucket comes with the project. `.env` keeps CLIPFORGE_BLOB_STORE=local
    # and a unit test asserts it, because routine development must not upload to
    # — or be billed for — a real bucket. Uploading is what -Live buys.
    $bucket = $config['CLIPFORGE_FIREBASE_STORAGE_BUCKET']
    if (-not [string]::IsNullOrWhiteSpace($bucket)) {
        $env:CLIPFORGE_BLOB_STORE = 'firebase'
    }

    Write-Host "  target    $projectId (LIVE)" -ForegroundColor Yellow
    Write-Host "  key       $credentials"
    if ([string]::IsNullOrWhiteSpace($bucket)) {
        Write-Host '  clips     stay on this machine (no CLIPFORGE_FIREBASE_STORAGE_BUCKET set)'
    }
    else {
        Write-Host "  clips     uploaded to $bucket for review"
    }
}
else {
    $env:CLIPFORGE_USE_EMULATORS = 'true'
    Write-Host '  target    local Emulator Suite'
    Write-Host '  hint      pass -Live to run against the real project'
}

Write-Host ''

$arguments = @('run', '--directory', 'apps/worker', 'clipforge-worker', 'run')
if ($Once) { $arguments += '--once' }

& uv @arguments
exit $LASTEXITCODE
