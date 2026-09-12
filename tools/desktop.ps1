<#
.SYNOPSIS
    Build the ClipForge desktop shell and put a shortcut on the Desktop.

.DESCRIPTION
    The desktop app is the same Angular build the PWA deploys, wrapped in Tauri
    and pointed at the real Firebase project. It exists because of one thing the
    phone cannot do: it runs on the machine that rendered the clips, so the local
    file server resolves and the video actually plays. On a phone the same code
    falls through to the poster frame.
    See docs/adr/0009-spark-tier-local-artefacts.md.

    Nothing here touches `apps/web/dist`, which is what `tools/deploy.ps1`
    publishes. The frontend is staged into `apps/desktop/frontend` with its own
    configuration, so a desktop build can never leak `127.0.0.1` playback or a
    disabled service worker onto the deployed site.

.PARAMETER Emulators
    Point the app at the local Emulator Suite instead of the real project.

.PARAMETER Bundle
    Also produce the NSIS installer. Slower, and downloads NSIS the first time.
    Without it you get the executable, which is all the shortcut needs.

.PARAMETER NoShortcut
    Skip creating or refreshing the Desktop shortcut.

.EXAMPLE
    pwsh -File tools/desktop.ps1
    pwsh -File tools/desktop.ps1 -Emulators
    pwsh -File tools/desktop.ps1 -Bundle
#>
[CmdletBinding()]
param(
    [switch] $Emulators,
    [switch] $Bundle,
    [switch] $NoShortcut
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Write-Step($message) { Write-Host "`n=> $message" -ForegroundColor Cyan }
function Fail($message) { Write-Host "`n   x $message" -ForegroundColor Red; exit 1 }

# Rust is not part of `doctor`'s checks, because everything except this builds
# without it. Say so plainly rather than letting cargo fail three layers down.
if (-not (Get-Command cargo -ErrorAction SilentlyContinue)) {
    $cargoHome = Join-Path $env:USERPROFILE '.cargo\bin'
    if (Test-Path (Join-Path $cargoHome 'cargo.exe')) {
        $env:PATH = "$env:PATH;$cargoHome"
    }
    else {
        Fail 'cargo not found. Tauri needs the Rust toolchain: https://rustup.rs'
    }
}

Write-Step 'Building the Angular app'
npm --prefix apps/web run build
if ($LASTEXITCODE -ne 0) { Fail 'The web build failed.' }

Write-Step 'Staging the frontend for Tauri'
$stageArgs = @('scripts/stage-frontend.mjs')
if ($Emulators) { $stageArgs += '--emulators' }
Push-Location (Join-Path $repoRoot 'apps/desktop')
try {
    node @stageArgs
    if ($LASTEXITCODE -ne 0) { Fail 'Staging failed; nothing was built.' }

    Write-Step 'Building the desktop shell'
    if ($Bundle) {
        npx tauri build
    }
    else {
        npx tauri build --no-bundle
    }
    if ($LASTEXITCODE -ne 0) { Fail 'The Tauri build failed.' }
}
finally {
    Pop-Location
}

$exe = Join-Path $repoRoot 'apps/desktop/src-tauri/target/release/clipforge-desktop.exe'
if (-not (Test-Path $exe)) { Fail "Built executable not found at $exe" }

$sizeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "   $exe  ($sizeMb MB)"

if (-not $NoShortcut) {
    Write-Step 'Refreshing the Desktop shortcut'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $link = Join-Path $desktop 'ClipForge.lnk'

    # Pointed at the build output rather than an installed copy, so a rebuild is
    # picked up by the same shortcut without reinstalling anything.
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = $exe
    $shortcut.WorkingDirectory = Split-Path -Parent $exe
    $shortcut.IconLocation = "$exe,0"
    $shortcut.Description = 'ClipForge - review clips on the machine that renders them'
    $shortcut.Save()

    Write-Host "   $link"
}

$target = if ($Emulators) { 'the local Emulator Suite' } else { 'the real Firebase project' }
Write-Host ''
Write-Host "  Done. The desktop app points at $target." -ForegroundColor Green
if (-not $Emulators) {
    Write-Host '  Start the worker too, or the queue will be empty and clips will not play:'
    Write-Host '    pwsh -File tools/worker.ps1 -Live'
}
Write-Host ''
