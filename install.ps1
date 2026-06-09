#Requires -Version 5.1
<#
.SYNOPSIS
  Install cyclopsctl globally on Windows.

.DESCRIPTION
  Validates Python 3.10+, installs cyclopsctl from git or a local path via pip,
  then verifies the command and prints PATH remediation when needed.

  The default install path requires Python only and CURSOR_API_KEY at runtime.

.PARAMETER Source
  Install source: git (default) or local.

.PARAMETER GitUrl
  Git URL used when -Source git (default: project repository).

.PARAMETER LocalPath
  Directory containing pyproject.toml when -Source local (default: script root).
#>
[CmdletBinding()]
param(
    [ValidateSet("git", "local")]
    [string]$Source = "git",

    [string]$GitUrl = "git+https://github.com/OlaProeis/Cyclopsctl.git",

    [string]$LocalPath = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

function Resolve-Python {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Error "Python was not found on PATH. Install Python 3.10+ from https://www.python.org/downloads/ and enable 'Add to PATH'."
    }
    return $python.Source
}

function Test-PythonVersion {
    param([string]$PythonExe)
    & $PythonExe -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)"
    if ($LASTEXITCODE -ne 0) {
        $version = & $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
        Write-Error "Python 3.10+ is required (found $version)."
    }
}

function Get-InstallTarget {
    param([string]$Source)
    switch ($Source) {
        "git" { return $GitUrl }
        "local" { return $LocalPath }
        default { throw "Unsupported source: $Source" }
    }
}

$pythonExe = Resolve-Python
Test-PythonVersion -PythonExe $pythonExe

$target = Get-InstallTarget -Source $Source
Write-Host "Installing cyclopsctl from $Source..." -ForegroundColor Cyan
& $pythonExe -m pip install --upgrade $target
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$verifyArgs = @("-m", "cyclopsctl.installer", "--verify-only")

& $pythonExe @verifyArgs
exit $LASTEXITCODE
