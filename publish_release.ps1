# publish_release.ps1
# ──────────────────────────────────────────────────────────────────────
# Publishes the v2.9.0 release to https://github.com/DeroXP/ghostshell
# and uploads dist\GhostShell.exe as the release asset.
#
# Usage:
#   1. Generate a fresh PAT at https://github.com/settings/tokens
#      Required scope:
#         classic PAT       → `repo`  (full)
#         fine-grained PAT  → `Contents: Read & write` on the
#                              DeroXP/ghostshell repository
#   2. Paste the token below or set $env:GITHUB_PAT in PowerShell first.
#   3. From PowerShell:    .\publish_release.ps1
# ──────────────────────────────────────────────────────────────────────

param(
    [string] $Token = $env:GITHUB_PAT,
    [string] $Tag   = "2.9.1",
    [string] $Repo  = "DeroXP/ghostshell",
    [string] $NotesFile = $null
)

$ErrorActionPreference = "Stop"

$root      = Split-Path -Parent $MyInvocation.MyCommand.Path
$exePath   = Join-Path $root "dist\Vispora.exe"
# Pick the notes file: explicit -NotesFile, then RELEASE_NOTES_<tag>.md,
# falling back to the most recent RELEASE_NOTES file in the project root.
if ($NotesFile -and (Test-Path $NotesFile)) {
    $notesPath = $NotesFile
} else {
    $candidate = Join-Path $root "RELEASE_NOTES_$Tag.md"
    if (Test-Path $candidate) {
        $notesPath = $candidate
    } else {
        $notesPath = Join-Path $root "RELEASE_NOTES_$Tag.md"
    }
}

if (-not $Token) {
    Write-Host "ERROR: no token supplied. Set `$env:GITHUB_PAT or pass -Token <pat>." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $exePath)) {
    Write-Host "ERROR: $exePath not found. Run pyinstaller build.spec first." -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $notesPath)) {
    Write-Host "ERROR: $notesPath not found." -ForegroundColor Red
    exit 1
}

# Use ReadAllText to get a *plain string*.  Get-Content returns a PSObject
# whose extra PSDrive/PSPath properties bleed into ConvertTo-Json output and
# trip GitHub's payload validator.
$body = [System.IO.File]::ReadAllText($notesPath, [System.Text.Encoding]::UTF8)

$h = @{
    "Authorization"       = "Bearer $Token"
    "Accept"              = "application/vnd.github+json"
    "X-GitHub-Api-Version"= "2022-11-28"
}

# ── Sanity-check token + repo ────────────────────────────────────────
Write-Host "→ Checking token..."
try {
    $u = Invoke-RestMethod "https://api.github.com/user" -Headers $h -TimeoutSec 15
    Write-Host "  Authenticated as: $($u.login)" -ForegroundColor Green
} catch {
    Write-Host "  Token rejected (HTTP $($_.Exception.Response.StatusCode.value__)). Generate a fresh PAT." -ForegroundColor Red
    exit 1
}

# ── Does the tag/release already exist?  If so, abort to avoid overwriting. ──
Write-Host "→ Checking for existing release at tag '$Tag'..."
try {
    $existing = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/tags/$Tag" -Headers $h -TimeoutSec 15
    Write-Host "  Release already exists: $($existing.html_url)" -ForegroundColor Yellow
    Write-Host "  Delete it on the web UI first, or change `$Tag in this script." -ForegroundColor Yellow
    exit 1
} catch {
    if ($_.Exception.Response.StatusCode.value__ -ne 404) { throw }
    Write-Host "  Tag is free to use." -ForegroundColor Green
}

# ── Create the release ───────────────────────────────────────────────
Write-Host "→ Creating release $Tag..."
$payload = @{
    tag_name   = $Tag
    name       = "v$Tag"
    body       = $body
    draft      = $false
    prerelease = $false
} | ConvertTo-Json -Depth 4

# Force UTF-8 bytes — Invoke-RestMethod's default string body uses
# cp1252 on EN-US Windows, which corrupts em-dashes / smart quotes
# in the markdown and trips GitHub's "Problems parsing JSON" check.
$payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)

$rel = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases" `
    -Headers $h -Method POST -Body $payloadBytes `
    -ContentType "application/json; charset=utf-8" -TimeoutSec 30
Write-Host "  Created: $($rel.html_url)" -ForegroundColor Green

# ── Upload the .exe asset ────────────────────────────────────────────
$exeBytes = [System.IO.File]::ReadAllBytes($exePath)
$exeName  = [System.IO.Path]::GetFileName($exePath)
$uploadUrl = $rel.upload_url -replace '\{.*\}$', "?name=$exeName"

Write-Host "→ Uploading $exeName ($([Math]::Round($exeBytes.Length / 1MB, 1)) MB)..."
$uh = @{
    "Authorization" = "Bearer $Token"
    "Accept"        = "application/vnd.github+json"
    "Content-Type"  = "application/octet-stream"
}
$asset = Invoke-RestMethod $uploadUrl -Headers $uh -Method POST -Body $exeBytes -TimeoutSec 600
Write-Host "  Uploaded: $($asset.browser_download_url)" -ForegroundColor Green

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "Release v$Tag published successfully!" -ForegroundColor Cyan
Write-Host "  $($rel.html_url)" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
