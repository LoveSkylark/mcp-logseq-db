<#
.SYNOPSIS
    Run the test suite locally or in Docker.

.DESCRIPTION
    Local runs have been unreliable for two reasons this script removes:

      1. A pip-installed copy of the package can shadow src/, so a run may
         exercise an old build. This clears stale bytecode and reports which
         copy was imported.
      2. Three interpreters (3.11/3.12/3.13) have written into __pycache__.
         Mixed-version bytecode produces failures that vanish on a second run.

    Docker runs sidestep both entirely, at the cost of a build.

.EXAMPLE
    .\scripts\test.ps1
    .\scripts\test.ps1 -Docker
    .\scripts\test.ps1 -Python 3.11
    .\scripts\test.ps1 -Live          # includes tests needing a live Logseq
#>
[CmdletBinding()]
param(
    [switch]$Docker,
    [switch]$Live,
    [switch]$Clean,
    [string]$Python = "3.13",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root

try {
    $marker = if ($Live) { @() } else { @("-m", "not live") }

    if ($Docker) {
        Write-Host "Building test image (Python $Python)..." -ForegroundColor Cyan
        docker build -f Dockerfile.test --build-arg "PYTHON_VERSION=$Python" `
            -t "mcp-logseq-db-tests:$Python" .
        if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

        # Mount the working tree so edits are picked up without a rebuild.
        # conftest.py puts /app/src first, so the mount is what gets tested.
        docker run --rm `
            -v "${root}:/app:ro" `
            -e PYTHONDONTWRITEBYTECODE=1 `
            "mcp-logseq-db-tests:$Python" `
            python -m pytest -q @marker @PytestArgs
        exit $LASTEXITCODE
    }

    if ($Clean) {
        Write-Host "Removing stale bytecode..." -ForegroundColor Cyan
        Get-ChildItem -Path $root -Include "__pycache__" -Recurse -Directory |
            Remove-Item -Recurse -Force
        Get-ChildItem -Path $root -Include "*.pyc" -Recurse -File |
            Remove-Item -Force
        Get-ChildItem -Path $root -Include ".pytest_cache" -Recurse -Directory |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }

    # `py -X -m pytest` rather than a bare `pytest`: the launcher form pins the
    # interpreter, and -m guarantees the pytest that runs belongs to it.
    Write-Host "Running tests with Python $Python..." -ForegroundColor Cyan
    & py "-$Python" -m pytest -q @marker @PytestArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
