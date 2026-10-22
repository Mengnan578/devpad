<#
.SYNOPSIS
    Install devpad's skill for agent hosts on Windows.

.DESCRIPTION
    Windows does not allow unprivileged symlinks by default, so this copies the skill
    directory instead of linking it. The copy is small (one Markdown file) and the
    engine stays in the package, so updating the package updates the behaviour.

    Claude Code users have a simpler path:
        /plugin marketplace add Mengnan578/devpad
        /plugin install devpad

.PARAMETER Cli
    Also write a `devpad.cmd` shim and add its directory to the user PATH.

.PARAMETER DryRun
    Show what would happen without changing anything.
#>
[CmdletBinding()]
param(
    [switch]$Cli,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Skill = Join-Path $Root "skills\devpad"

if (-not (Test-Path (Join-Path $Skill "SKILL.md"))) {
    Write-Error "missing $Skill\SKILL.md - is this the devpad package root?"
    exit 1
}

Write-Host "skill source: $Skill" -ForegroundColor Cyan
Write-Host "agent skill roots:"

$roots = @(
    (Join-Path $env:USERPROFILE ".claude\skills"),
    (Join-Path $env:USERPROFILE ".dsh\skills"),
    (Join-Path $env:USERPROFILE ".agents\skills")
)

foreach ($base in $roots) {
    $parent = Split-Path -Parent $base
    if (-not (Test-Path $parent)) {
        Write-Host "  skip  $base  (parent does not exist)"
        continue
    }
    $target = Join-Path $base "devpad"
    if ($DryRun) {
        Write-Host "  plan  copy $Skill -> $target"
        continue
    }
    New-Item -ItemType Directory -Force -Path $base | Out-Null
    if (Test-Path $target) { Remove-Item -Recurse -Force $target }
    Copy-Item -Recurse -Force $Skill $target
    Write-Host "  copy  $target"
}

if ($Cli) {
    Write-Host "command line:"
    $bin = Join-Path $env:USERPROFILE ".local\bin"
    $shim = Join-Path $bin "devpad.cmd"
    if ($DryRun) {
        Write-Host "  plan  $shim"
    } else {
        New-Item -ItemType Directory -Force -Path $bin | Out-Null
        # Absolute path, resolved now: the shim lives in ~/.local/bin and must not
        # guess its way back to the package.
        $engine = Join-Path $Root "scripts\devpad.py"
        @"
@echo off
rem devpad - installed by install.ps1
python "$engine" %*
"@ | Set-Content -Path $shim -Encoding ASCII
        Write-Host "  write $shim"

        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($userPath -notlike "*$bin*") {
            if (-not $DryRun) {
                [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
                Write-Host "  add   $bin to the user PATH (open a new terminal to pick it up)"
            }
        } else {
            Write-Host "  ok    $bin is already on PATH"
        }
    }
}

Write-Host ""
Write-Host "next" -ForegroundColor Cyan
Write-Host "  cd <your project>"
if ($Cli) {
    Write-Host "  devpad doctor"
    Write-Host "  devpad up --terminal"
} else {
    Write-Host "  python $Root\scripts\devpad.py doctor"
    Write-Host "  python $Root\scripts\devpad.py up --terminal"
}
Write-Host ""
Write-Host "  then ask your agent to 'restart the backend'."
Write-Host ""
Write-Host "note: opening windows requires Windows Terminal (wt.exe)." -ForegroundColor Yellow
Write-Host "      The logic is covered by CI, but window opening is not verified" -ForegroundColor Yellow
Write-Host "      on a real Windows machine yet." -ForegroundColor Yellow
