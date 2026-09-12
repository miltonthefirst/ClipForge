<#
.SYNOPSIS
    Apply ClipForge's clip-retention rule to the Cloud Storage bucket.

.DESCRIPTION
    Clips are uploaded so a phone can review them, and are meant to disappear
    shortly afterwards. The deletion is done by Cloud Storage itself, through an
    Object Lifecycle Management rule — not by a cron job, and not by the worker.

    That is a deliberate choice rather than a shortcut. A scheduled task on the
    worker would only expire clips while the worker happened to be running,
    which is the opposite of when an unattended bucket needs watching. Lifecycle
    rules are evaluated server-side, cost nothing, and cannot be forgotten.

    The retention window lives in one place — CLIPFORGE_CLIP_RETENTION_DAYS in
    `.env` — because two places would eventually disagree. This script reads it,
    writes it into the rule, and applies it. The worker stamps the same number
    onto every clip as `playbackExpiresAt`, so the app knows a video has gone
    without asking the bucket.

.PARAMETER DryRun
    Print the rule that would be applied, and the bucket it would be applied to,
    without changing anything.

.EXAMPLE
    pwsh -File tools/storage-lifecycle.ps1
    pwsh -File tools/storage-lifecycle.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($message) { Write-Host "`n=> $message" -ForegroundColor Cyan }
function Fail($message) { Write-Host "`n   x $message" -ForegroundColor Red; exit 1 }

# ── Configuration, from .env and nowhere else ────────────────────────────────
$envPath = Join-Path $repoRoot '.env'
if (-not (Test-Path $envPath)) { Fail ".env not found. Copy .env.example to .env first." }

$config = @{}
foreach ($line in Get-Content $envPath) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }
    $config[$trimmed.Substring(0, $split).Trim()] = $trimmed.Substring($split + 1).Trim()
}

$bucket = $config['CLIPFORGE_FIREBASE_STORAGE_BUCKET']
if ([string]::IsNullOrWhiteSpace($bucket)) {
    Fail @"
CLIPFORGE_FIREBASE_STORAGE_BUCKET is not set in .env.

It is the bucket name from the Firebase console (Storage -> Files), and looks
like `your-project.firebasestorage.app`.
"@
}

$credentials = $config['CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS']
if ([string]::IsNullOrWhiteSpace($credentials) -or -not (Test-Path $credentials)) {
    Fail @"
No service account key found. Applying a lifecycle rule writes to the bucket, so
it needs the same credential the worker uses.

Set CLIPFORGE_GOOGLE_APPLICATION_CREDENTIALS in .env to a key generated under
  Project settings -> Service accounts -> Generate new private key
"@
}

$days = $config['CLIPFORGE_CLIP_RETENTION_DAYS']
if ([string]::IsNullOrWhiteSpace($days)) { $days = '5' }
if ($days -notmatch '^\d+$' -or [int]$days -lt 1) {
    Fail "CLIPFORGE_CLIP_RETENTION_DAYS must be a whole number of days; got '$days'."
}

# ── Apply, through the worker ────────────────────────────────────────────────
#
# Not through gcloud. The Google Cloud CLI is a large install that this project
# otherwise has no use for, and the worker already holds a service-account
# credential for this bucket — so the tool that knows the retention setting is
# also the one that can write it. `clipforge-worker retention` is the
# implementation; this script only supplies the live environment, exactly as
# tools/worker.ps1 does.

$env:CLIPFORGE_USE_EMULATORS = 'false'

Write-Host ''
Write-Host '  ClipForge storage lifecycle' -ForegroundColor White
Write-Host '  ---------------------------'
Write-Host "  bucket     gs://$bucket"
Write-Host "  retention  $days day(s) after upload, for objects under clips/"
Write-Host ''

$arguments = @('run', '--directory', 'apps/worker', 'clipforge-worker', 'retention')
if (-not $DryRun) { $arguments += '--apply' }

& uv @arguments
$code = $LASTEXITCODE

if ($DryRun) {
    Write-Host ''
    Write-Host '  (dry run, nothing was applied)' -ForegroundColor Yellow
    # A dry run reports whether the bucket already agrees, and "it does not" is
    # information rather than a failure.
    exit 0
}

if ($code -ne 0) { Fail 'Could not apply the lifecycle rule.' }

Write-Host ''
Write-Host "  Done. Clips now expire $days day(s) after upload." -ForegroundColor Green
