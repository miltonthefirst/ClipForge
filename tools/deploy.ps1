<#
.SYNOPSIS
    Deploy ClipForge's Firestore rules, indexes and PWA to its Firebase project.

.DESCRIPTION
    Every Firebase deploy REPLACES rather than merges. This account also hosts
    `miltongore`, a live production site, so a mistyped or ambient project id is
    not a typo — it is an outage on something unrelated.

    This script therefore refuses to produce a bare `firebase deploy`. The
    project id comes from .env and is passed explicitly, the targets are always
    named with --only, and the exact command is printed before it runs.

    It also refuses to deploy a PWA build that is still pointed at the local
    emulators. That failure is otherwise silent and remote: the site loads, and
    then every read hangs against 127.0.0.1:8080 on a phone that has no worker.

.PARAMETER Only
    Which targets to deploy: rules, indexes, hosting. Defaults to all three.
    Storage rules are deliberately NOT deployable here — the Spark free tier has
    no bucket at all, so `firebase deploy --only storage` fails by definition.
    See docs/adr/0009-spark-tier-local-artefacts.md.

.PARAMETER DryRun
    Print the command that would run, and stop. Also renders the config that
    would be inlined, so it can be checked before anything is published.

.PARAMETER BuildOnly
    Build the PWA and inline the configuration, then stop without deploying.
    Leaves the finished artefact in apps/web/dist/web/browser for inspection.
    -DryRun exits before the build, so this is the only way to see what would
    actually be published.

.PARAMETER Yes
    Skip the confirmation prompt. For CI; think twice interactively.

.EXAMPLE
    pwsh -File tools/deploy.ps1 -Only rules,indexes -DryRun
    pwsh -File tools/deploy.ps1
#>
[CmdletBinding()]
param(
    # A plain string rather than a [string[]] with ValidateSet. Under -File,
    # PowerShell splits "rules,indexes" into an array before binding, which a
    # ValidateSet on [string[]] then rejects — and which stringifies to
    # "rules indexes", space-joined, when bound to [string]. Accepting a string
    # and splitting on either separator below binds the same under -File and
    # -Command, which is the only property that matters here.
    [string] $Only = 'rules,indexes,hosting',

    [switch] $DryRun,
    [switch] $BuildOnly,
    [switch] $Yes
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$known = @('rules', 'indexes', 'hosting')

# Into a NEW variable, never back into $Only. `param([string] $Only)` type-
# constrains that variable for the whole script, so assigning an array to it
# coerces straight back to a space-joined string — silently, and the split then
# looks like it did nothing.
$targetsWanted = @($Only -split '[,\s]+' | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ -ne '' })

$unknown = @($targetsWanted | Where-Object { $known -notcontains $_ })
if ($unknown.Count -gt 0) {
    Write-Host "Unknown target(s): $($unknown -join ', '). Valid: $($known -join ', ')." -ForegroundColor Red
    exit 1
}
if ($targetsWanted.Count -eq 0) {
    Write-Host 'No targets selected. Refusing to deploy nothing.' -ForegroundColor Red
    exit 1
}

function Write-Step($message) { Write-Host "`n=> $message" -ForegroundColor Cyan }
function Write-Warn($message) { Write-Host "   ! $message" -ForegroundColor Yellow }
function Fail($message) { Write-Host "`n   x $message" -ForegroundColor Red; exit 1 }

# ── Configuration ────────────────────────────────────────────────────────────

$envPath = Join-Path $repoRoot '.env'
if (-not (Test-Path $envPath)) {
    Fail ".env not found. Copy .env.example to .env and fill in the project's values."
}

$config = @{}
foreach ($line in Get-Content $envPath) {
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) { continue }
    $config[$trimmed.Substring(0, $split).Trim()] = $trimmed.Substring($split + 1).Trim()
}

$projectId = $config['CLIPFORGE_FIREBASE_PROJECT_ID']
if ([string]::IsNullOrWhiteSpace($projectId)) {
    Fail 'CLIPFORGE_FIREBASE_PROJECT_ID is not set in .env. Refusing to guess a project.'
}

# The one project this repository must never touch. Named explicitly rather than
# left to a --project flag being right, because being right every time is the
# assumption that eventually fails.
if ($projectId -eq 'miltongore') {
    Fail "CLIPFORGE_FIREBASE_PROJECT_ID is 'miltongore', which is a live production site. Refusing."
}

# ── The PWA's client config ──────────────────────────────────────────────────

$deployHosting = $targetsWanted -contains 'hosting'
$webConfig = $null

if ($deployHosting) {
    $apiKey = $config['CLIPFORGE_WEB_API_KEY']
    $authDomain = $config['CLIPFORGE_WEB_AUTH_DOMAIN']
    $appId = $config['CLIPFORGE_WEB_APP_ID']
    $localOrigin = $config['CLIPFORGE_WEB_LOCAL_SERVER_ORIGIN']
    if ([string]::IsNullOrWhiteSpace($localOrigin)) { $localOrigin = 'http://127.0.0.1:8765' }

    $missing = @()
    if ([string]::IsNullOrWhiteSpace($apiKey)) { $missing += 'CLIPFORGE_WEB_API_KEY' }
    if ([string]::IsNullOrWhiteSpace($authDomain)) { $missing += 'CLIPFORGE_WEB_AUTH_DOMAIN' }
    if ([string]::IsNullOrWhiteSpace($appId)) { $missing += 'CLIPFORGE_WEB_APP_ID' }

    if ($missing.Count -gt 0) {
        Fail @"
Cannot deploy hosting: $($missing -join ', ') not set in .env.

Without these the built app keeps its development default, which points at the
local Emulator Suite — so the deployed site would load and then hang on every
read, from a phone that has no emulator to reach.

Get them from the Firebase console:
  Project settings -> Your apps -> Web app -> SDK setup and configuration
Register a Web app first if there is not one yet.
"@
    }

    # Built by hand rather than with ConvertTo-Json so the emitted block stays
    # readable in view-source, which is where anyone debugging a live site looks.
    $webConfig = @"
      // Merged rather than assigned, matching src/index.html: anything set
      // before this script runs must survive it.
      window.__clipforge = Object.assign({}, window.__clipforge, {
        useEmulators: false,
        firebase: {
          projectId: '$projectId',
          apiKey: '$apiKey',
          authDomain: '$authDomain',
          appId: '$appId',
        },
        localServerOrigin: '$localOrigin',
      });
"@
}

# ── What is about to happen ──────────────────────────────────────────────────

$targets = @()
if ($targetsWanted -contains 'rules') { $targets += 'firestore:rules' }
if ($targetsWanted -contains 'indexes') { $targets += 'firestore:indexes' }
if ($deployHosting) { $targets += 'hosting' }
$onlyArg = $targets -join ','

Write-Host ''
Write-Host '  ClipForge deploy' -ForegroundColor White
Write-Host '  ----------------'
Write-Host "  project   $projectId"
Write-Host "  targets   $onlyArg"
Write-Host "  account   $(npx --yes firebase login:list 2>$null | Select-String 'Logged in as' | ForEach-Object { $_.ToString().Trim() })"
Write-Host ''

if ($deployHosting) {
    Write-Host '  index.html will carry:' -ForegroundColor White
    Write-Host $webConfig
    Write-Host ''
}

if ($DryRun) {
    Write-Host '  Would run:' -ForegroundColor White
    Write-Host "    npx firebase deploy --project $projectId --only $onlyArg"
    Write-Host ''
    Write-Host '  (dry run, nothing was deployed)' -ForegroundColor Yellow
    exit 0
}

if (-not $Yes -and -not $BuildOnly) {
    $answer = Read-Host "  Deploy $onlyArg to $projectId? [y/N]"
    if ($answer -ne 'y' -and $answer -ne 'Y') {
        Write-Host '  Cancelled.' -ForegroundColor Yellow
        exit 0
    }
}

# ── Build ────────────────────────────────────────────────────────────────────

if ($deployHosting) {
    Write-Step 'Building the PWA'
    npm --prefix apps/web run build
    if ($LASTEXITCODE -ne 0) { Fail 'The web build failed; nothing was deployed.' }

    Write-Step 'Inlining the project configuration'
    $indexPath = Join-Path $repoRoot 'apps/web/dist/web/browser/index.html'
    if (-not (Test-Path $indexPath)) { Fail "Built index.html not found at $indexPath" }

    $html = Get-Content $indexPath -Raw
    $pattern = '(?s)/\* CLIPFORGE-CONFIG-START \*/.*?/\* CLIPFORGE-CONFIG-END \*/'
    if ($html -notmatch $pattern) {
        Fail @"
Could not find the CLIPFORGE-CONFIG markers in the built index.html.

They are what this script replaces, so without them the deployed app would
silently keep its emulator defaults. Restore the markers in
apps/web/src/index.html rather than removing this check.
"@
    }

    $replacement = "/* CLIPFORGE-CONFIG-START */`n$webConfig`n      /* CLIPFORGE-CONFIG-END */"
    # A script literal, so `$&` and friends in the replacement must not be
    # interpreted as regex backreferences.
    $updated = [System.Text.RegularExpressions.Regex]::Replace(
        $html, $pattern, [System.Text.RegularExpressions.MatchEvaluator] { $replacement })
    Set-Content -Path $indexPath -Value $updated -Encoding utf8 -NoNewline

    if ((Get-Content $indexPath -Raw) -notmatch [regex]::Escape("projectId: '$projectId'")) {
        Fail 'The configuration did not land in index.html. Refusing to deploy an unconfigured build.'
    }
    Write-Host "   configured for $projectId"

    # The service worker validates every asset against a hash recorded at build
    # time, and index.html was just edited. Without regenerating the manifest the
    # hash no longer matches, and the worker refuses the app shell it is supposed
    # to be serving — offline stops working, and the first sign of it is a user
    # with no network and a blank page.
    Write-Step 'Regenerating the service worker manifest'
    Push-Location (Join-Path $repoRoot 'apps/web')
    try {
        npx ngsw-config dist/web/browser ngsw-config.json /
        if ($LASTEXITCODE -ne 0) { Fail 'ngsw-config failed; refusing to deploy a stale manifest.' }
    }
    finally {
        Pop-Location
    }

    $manifestPath = Join-Path $repoRoot 'apps/web/dist/web/browser/ngsw.json'
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.index -ne '/index.html') {
        # Seen for real: run this from Git Bash and MSYS rewrites the '/' base
        # href into a Windows path, so every entry is hashed under
        # 'C:/Program Files/Git/...' and the manifest is silently useless.
        Fail "ngsw.json has index '$($manifest.index)', expected '/index.html'. Run this from PowerShell, not a POSIX shell."
    }
    Write-Host '   manifest regenerated'
}

if ($BuildOnly) {
    Write-Host ''
    Write-Host '  Built and configured, nothing deployed.' -ForegroundColor Yellow
    Write-Host '  Inspect: apps/web/dist/web/browser/index.html'
    Write-Host ''
    exit 0
}

# ── Deploy ───────────────────────────────────────────────────────────────────

Write-Step "Deploying $onlyArg to $projectId"
npx firebase deploy --project $projectId --only $onlyArg
if ($LASTEXITCODE -ne 0) { Fail "Deploy failed with exit code $LASTEXITCODE." }

Write-Host ''
Write-Host "  Done. https://$projectId.web.app" -ForegroundColor Green
Write-Host ''
