<#
.SYNOPSIS
    Run the test suite locally or in a container.

.DESCRIPTION
    Local runs have been unreliable for two reasons this script removes:

      1. A pip-installed copy of the package can shadow src/, so a run may
         exercise an old build. tests/conftest.py forces src to the front of
         sys.path and prints which copy was imported; -Clean removes stale
         bytecode.
      2. Three interpreters (3.11/3.12/3.13) have written into __pycache__.
         Mixed-version bytecode produces failures that vanish on a rerun.

    Container runs sidestep both. Docker and Podman are both supported; the
    engine is auto-detected unless one is named explicitly.

.EXAMPLE
    .\scripts\test.ps1                      # local
    .\scripts\test.ps1 -Clean               # local, wipe bytecode first
    .\scripts\test.ps1 -Container           # auto-detect podman or docker
    .\scripts\test.ps1 -Podman              # force podman
    .\scripts\test.ps1 -Docker              # force docker
    .\scripts\test.ps1 -Container -Python 3.11
    .\scripts\test.ps1 -Live                # include tests needing Logseq
    .\scripts\test.ps1 -k outline -vv       # extra args go to pytest
#>
[CmdletBinding()]
param(
    [switch]$Container,
    [switch]$Docker,
    [switch]$Podman,
    [switch]$Live,
    [switch]$Clean,
    [switch]$Rebuild,
    [string]$Python = "3.13",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$image = "mcp-logseq-db-tests:$Python"

function Resolve-Engine {
    <#
        Return the container command to use.

        An explicit -Docker or -Podman wins even when both are installed, since
        a machine with both usually has a reason. Otherwise Podman is preferred:
        it is rootless by default, so a container writing into a mounted volume
        cannot leave root-owned files behind.
    #>
    if ($Docker -and $Podman) {
        throw "Specify -Docker or -Podman, not both."
    }
    if ($Docker) {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            throw "-Docker was specified but docker is not on PATH."
        }
        return "docker"
    }
    if ($Podman) {
        if (-not (Get-Command podman -ErrorAction SilentlyContinue)) {
            throw "-Podman was specified but podman is not on PATH."
        }
        return "podman"
    }

    $found = @("podman", "docker") |
        Where-Object { Get-Command $_ -ErrorAction SilentlyContinue }
    if (-not $found) {
        throw ("Neither podman nor docker is on PATH. Run without " +
               "-Container to test locally.")
    }
    return $found[0]
}

function Test-EngineReady($engine) {
    <#
        Both engines fail confusingly when the daemon or machine is down:
        Docker reports a pipe error, Podman a missing connection. Turn either
        into one clear instruction.
    #>
    & $engine info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { return }

    if ($engine -eq "podman") {
        throw ("podman is installed but its machine is not running. " +
               "Start it with: podman machine start")
    }
    throw ("docker is installed but the daemon is not responding. " +
           "Start Docker Desktop and try again.")
}

Push-Location $root
try {
    $marker = if ($Live) { @() } else { @("-m", "not live") }
    $useContainer = $Container -or $Docker -or $Podman

    if ($useContainer) {
        $engine = Resolve-Engine
        Test-EngineReady $engine
        Write-Host "Using $engine (Python $Python)" -ForegroundColor Cyan

        $exists = (& $engine images -q $image 2>$null)
        if ($Rebuild -or -not $exists) {
            Write-Host "Building image..." -ForegroundColor Cyan
            & $engine build -f Dockerfile.test `
                --build-arg "PYTHON_VERSION=$Python" -t $image .
            if ($LASTEXITCODE -ne 0) { throw "$engine build failed" }
        }
        else {
            Write-Host "Reusing image ($image); pass -Rebuild to force." `
                -ForegroundColor DarkGray
        }

        # The tree is mounted read-only so the run exercises your edits without
        # a rebuild, and cannot write .pyc or test artifacts back into it.
        # conftest.py puts /app/src first, so the mount is what gets tested
        # rather than the copy baked into the image.
        #
        # Podman relabels volumes for SELinux on some hosts; :ro,z keeps it
        # working there. Docker rejects the z flag on Windows, so the mount
        # string differs per engine.
        $mount = if ($engine -eq "podman") { "${root}:/app:ro,z" }
                 else { "${root}:/app:ro" }

        # cache_dir is redirected because the mount is read-only: pytest
        # otherwise tries to write .pytest_cache into the source tree and
        # warns twice per run. -p no:cacheprovider would also work but loses
        # --lf and --ff inside the container.
        & $engine run --rm `
            -v $mount `
            -e PYTHONDONTWRITEBYTECODE=1 `
            $image `
            python -m pytest -q -o cache_dir=/tmp/pytest_cache `
            @marker @PytestArgs
        exit $LASTEXITCODE
    }

    if ($Clean) {
        Write-Host "Removing stale bytecode..." -ForegroundColor Cyan
        Get-ChildItem -Path $root -Include "__pycache__" -Recurse -Directory |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        Get-ChildItem -Path $root -Include "*.pyc" -Recurse -File |
            Remove-Item -Force -ErrorAction SilentlyContinue
        Get-ChildItem -Path $root -Include ".pytest_cache" -Recurse -Directory |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }

    # `py -X -m pytest` rather than a bare `pytest`: the launcher pins the
    # interpreter, and -m guarantees the pytest that runs belongs to it.
    Write-Host "Running tests with Python $Python..." -ForegroundColor Cyan
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py "-$Python" -m pytest -q @marker @PytestArgs
    }
    else {
        & python3 -m pytest -q @marker @PytestArgs
    }
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
