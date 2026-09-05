<#
.SYNOPSIS
    Install or upgrade mcp-logseq-db from this working tree.

.DESCRIPTION
    Installs into the interpreter an MCP client will actually launch, and
    reports what changed. Two failure modes this avoids:

      1. Installing into a different interpreter than the one Claude Desktop
         runs. The console script then exists but the client cannot find it,
         or finds an older one.
      2. A non-editable install shadowing src/ during development. -Editable
         links the working tree instead, so edits take effect without
         reinstalling.

    After installing it verifies the package imports and the console script
    resolves, because pip reporting success is not the same as the server
    being launchable.

.EXAMPLE
    .\scripts\install.ps1                 # install or upgrade
    .\scripts\install.ps1 -Editable       # development install
    .\scripts\install.ps1 -Python 3.11
    .\scripts\install.ps1 -Uninstall
    .\scripts\install.ps1 -WhatIfOnly     # show what would happen
#>
[CmdletBinding()]
param(
    [switch]$Editable,
    [switch]$Uninstall,
    [switch]$WhatIfOnly,
    [switch]$Quiet,
    [string]$Python = "3.13"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$package = "mcp-logseq-db"
$module = "mcp_logseq_db"

function Say($message, $colour = "Cyan") {
    if (-not $Quiet) { Write-Host $message -ForegroundColor $colour }
}

function Detail($message) {
    if (-not $Quiet) { Write-Host "  $message" -ForegroundColor DarkGray }
}

function Resolve-Python {
    <#
        Return the argument list that invokes the chosen interpreter.

        `py -3.13` is preferred on Windows because it pins the version
        explicitly; a bare `python` may be whichever one is first on PATH,
        which is exactly the ambiguity that leads to installing into the wrong
        place.
    #>
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py "-$Python" -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) {
            return @{ Command = "py"; Args = @("-$Python") }
        }
        Write-Host "Python $Python not found via the py launcher; falling back." `
            -ForegroundColor Yellow
    }
    foreach ($candidate in @("python3", "python")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            return @{ Command = $candidate; Args = @() }
        }
    }
    throw "No Python interpreter found. Install Python $Python or newer."
}

function Invoke-Py {
    param([string[]]$Arguments)
    & $py.Command @($py.Args + $Arguments)
}

function Get-InstalledVersion {
    $output = Invoke-Py @("-m", "pip", "show", $package) 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    $line = $output | Where-Object { $_ -match "^Version:" }
    if (-not $line) { return $null }
    return ($line -replace "^Version:\s*", "").Trim()
}

function Get-ProjectVersion {
    $toml = Get-Content (Join-Path $root "pyproject.toml")
    $line = $toml | Where-Object { $_ -match '^\s*version\s*=' } | Select-Object -First 1
    if (-not $line) { return "unknown" }
    return ($line -replace '.*=\s*"?([^"]*)"?.*', '$1').Trim()
}

Push-Location $root
try {
    $py = Resolve-Python
    $interpreter = (Invoke-Py @("-c", "import sys; print(sys.executable)")).Trim()
    $pyVersion = (Invoke-Py @("-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))")).Trim()

    Say "Interpreter: $interpreter (Python $pyVersion)"

    $before = Get-InstalledVersion
    $target = Get-ProjectVersion

    # ---------------------------------------------------------- uninstall
    if ($Uninstall) {
        if (-not $before) {
            Say "$package is not installed for this interpreter." "Yellow"
            exit 0
        }
        if ($WhatIfOnly) {
            Say "Would uninstall $package $before." "Yellow"
            exit 0
        }
        Invoke-Py @("-m", "pip", "uninstall", "-y", $package)
        Say "Uninstalled $package $before." "Green"
        exit $LASTEXITCODE
    }

    # ------------------------------------------------------------ install
    if ($before) {
        Detail "Installed: $before  ->  project: $target"
    }
    else {
        Detail "Not currently installed; project version is $target"
    }

    $mode = if ($Editable) { "editable" } else { "regular" }
    if ($WhatIfOnly) {
        Say "Would perform a $mode install of $target from $root." "Yellow"
        exit 0
    }

    # An editable install replacing a regular one (or the reverse) is cleaner
    # after an explicit uninstall: pip can otherwise leave the previous
    # dist-info behind, and `pip show` then reports a version that is not what
    # is being imported.
    if ($before) {
        Detail "Removing the existing install first"
        Invoke-Py @("-m", "pip", "uninstall", "-y", $package) | Out-Null
    }

    Say "Installing ($mode)..."
    $spec = if ($Editable) { @("-e", ".") } else { @(".") }
    Invoke-Py (@("-m", "pip", "install", "--upgrade") + $spec)
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

    # --------------------------------------------------------- verify
    # pip reporting success is not the same as the server being launchable.
    Say "Verifying..."

    $after = Get-InstalledVersion
    if (-not $after) { throw "pip install succeeded but the package is not installed" }
    Detail "version: $after"

    $location = (Invoke-Py @("-c", "import $module, sys; print($module.__file__)")).Trim()
    if ($LASTEXITCODE -ne 0) { throw "the package installed but does not import" }
    Detail "imports from: $location"

    if ($Editable -and $location -notlike "$root*") {
        Write-Host ("  WARN editable install did not link the working tree; " +
                    "another copy may be shadowing it") -ForegroundColor Yellow
    }

    # The entry point is what an MCP client launches, so a working import is
    # not sufficient evidence.
    Invoke-Py @("-c", "from $module.server import main") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "the server entry point does not import" }
    Detail "entry point: ${module}.server:main"

    $console = Get-Command $package -ErrorAction SilentlyContinue
    if ($console) {
        Detail "console script: $($console.Source)"
    }
    else {
        Write-Host ("  WARN the '$package' console script is not on PATH. " +
                    "Use the module form in your MCP client config:") `
            -ForegroundColor Yellow
        # -join rather than Join-String: the latter is PowerShell 7+, and
        # Windows PowerShell 5.1 is still the default on many machines.
        $argList = ($py.Args + @("-m", "$module.server") |
            ForEach-Object { '"' + $_ + '"' }) -join ", "
        Write-Host "         { `"command`": `"$($py.Command)`", `"args`": [$argList] }" `
            -ForegroundColor Yellow
    }

    # ----------------------------------------------------------- summary
    if (-not $before) {
        Say "Installed $package $after." "Green"
    }
    elseif ($before -eq $after) {
        Say "Reinstalled $package $after (unchanged version)." "Green"
    }
    else {
        Say "Upgraded $package $before -> $after." "Green"
    }

    if (-not $Quiet) {
        Write-Host ""
        Write-Host "Restart your MCP client so it picks up the new build." `
            -ForegroundColor DarkGray
        if (-not $Editable) {
            Write-Host ("Tip: -Editable links the working tree, so source edits " +
                        "take effect without reinstalling.") -ForegroundColor DarkGray
        }
    }
}
catch {
    Write-Host "FAILED: $_" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
