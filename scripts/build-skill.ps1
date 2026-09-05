<#
.SYNOPSIS
    Build dist/logseq-db-native.zip from skills/logseq-db-native.

.DESCRIPTION
    Overwrites the existing zip. Before doing so it checks the things that
    silently produce a broken skill:

      - SKILL.md exists and has YAML frontmatter with a name and description.
        Claude will not load a skill whose frontmatter is missing or malformed,
        and the failure is quiet.
      - Every reference/*.md the skill links to actually exists. A dangling
        link is invisible until the model tries to follow it mid-task.
      - No stray files (.pyc, .DS_Store, editor backups) are shipped inside.

    The archive is written entry by entry rather than with Compress-Archive,
    because Compress-Archive records Windows path separators for nested
    directories. The ZIP specification requires forward slashes, so importers
    reject the result with "Zip file contains path with invalid characters" --
    a failure that only appears at import time, never at build time.

.EXAMPLE
    .\scripts\build-skill.ps1
    .\scripts\build-skill.ps1 -Check      # validate without writing
    .\scripts\build-skill.ps1 -Quiet
#>
[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Quiet,
    [string]$SkillName = "logseq-db-native"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root "skills\$SkillName"
$distDir = Join-Path $root "dist"
$zipPath = Join-Path $distDir "$SkillName.zip"

function Write-Step($message) {
    if (-not $Quiet) { Write-Host $message -ForegroundColor Cyan }
}

function Write-Ok($message) {
    if (-not $Quiet) { Write-Host "  OK   $message" -ForegroundColor DarkGray }
}

function Test-IsJunk($item) {
    return ($item.Name -match '\.(pyc|pyo|bak|orig|swp)$') -or
           ($item.Name -in @('.DS_Store', 'Thumbs.db')) -or
           ($item.FullName -match '[\\/]__pycache__[\\/]?')
}

try {
    if (-not (Test-Path $source)) {
        throw "Skill source not found: $source"
    }

    # --- frontmatter -----------------------------------------------------
    Write-Step "Validating $SkillName..."

    $skillFile = Join-Path $source "SKILL.md"
    if (-not (Test-Path $skillFile)) {
        throw "SKILL.md is missing from $source"
    }

    $lines = Get-Content $skillFile
    if ($lines[0] -ne "---") {
        throw "SKILL.md must start with '---' on line 1; found: '$($lines[0])'"
    }
    $closing = (1..($lines.Count - 1) |
        Where-Object { $lines[$_] -eq "---" } | Select-Object -First 1)
    if (-not $closing) {
        throw "SKILL.md frontmatter has no closing '---'"
    }
    $frontmatter = $lines[1..($closing - 1)]

    foreach ($key in @("name", "description")) {
        if (-not ($frontmatter -match "^${key}:")) {
            throw "SKILL.md frontmatter is missing '$key'"
        }
    }
    $declaredName = (($frontmatter | Where-Object { $_ -match "^name:" }) `
        -replace "^name:\s*", "" -replace '"', '').Trim()
    if ($declaredName -ne $SkillName) {
        throw ("SKILL.md declares name '$declaredName' but the folder is " +
               "'$SkillName'; Claude keys on the frontmatter name")
    }
    Write-Ok "frontmatter (name: $SkillName)"

    # --- reference links -------------------------------------------------
    $body = Get-Content $skillFile -Raw
    $referenced = [regex]::Matches($body, 'reference/([\w.-]+\.md)') |
        ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique

    $missing = @($referenced | Where-Object {
        -not (Test-Path (Join-Path $source "reference\$_")) })
    if ($missing) {
        throw ("SKILL.md links to reference files that do not exist: " +
               ($missing -join ', '))
    }
    if ($referenced) { Write-Ok "$($referenced.Count) reference file(s) resolve" }

    # Present but unlinked. Not fatal -- a reference can be reached from
    # another reference -- but usually means a rename was missed.
    $onDisk = @(Get-ChildItem (Join-Path $source "reference") -Filter *.md `
        -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    $orphans = @($onDisk | Where-Object { $referenced -notcontains $_ })
    if ($orphans -and -not $Quiet) {
        Write-Host ("  WARN reference files not linked from SKILL.md: " +
                    ($orphans -join ', ')) -ForegroundColor Yellow
    }

    # --- collect files ---------------------------------------------------
    $all = Get-ChildItem $source -Recurse -File -Force
    $junk = @($all | Where-Object { Test-IsJunk $_ })
    $files = @($all | Where-Object { -not (Test-IsJunk $_) })

    if ($junk -and -not $Quiet) {
        Write-Host "  WARN excluding stray files:" -ForegroundColor Yellow
        foreach ($item in $junk) {
            Write-Host "         $($item.FullName.Substring($source.Length + 1))" `
                -ForegroundColor Yellow
        }
    }
    if (-not $files) { throw "No files to package." }

    if ($Check) {
        Write-Host "Validation passed. Nothing written (-Check)." -ForegroundColor Green
        exit 0
    }

    # --- build -----------------------------------------------------------
    if (-not (Test-Path $distDir)) {
        New-Item -ItemType Directory -Path $distDir | Out-Null
    }
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    Add-Type -AssemblyName System.IO.Compression

    Write-Step "Building $SkillName.zip..."

    $archive = [System.IO.Compression.ZipFile]::Open(
        $zipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($file in $files) {
            # Path INSIDE the archive: rooted at the skill folder, with
            # forward slashes. Compress-Archive would write backslashes here,
            # which is what importers reject as invalid characters.
            $relative = $file.FullName.Substring($source.Length).TrimStart('\', '/')
            $entryName = "$SkillName/" + ($relative -replace '\\', '/')

            [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, $file.FullName, $entryName,
                [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
    finally {
        $archive.Dispose()
    }

    # --- verify ----------------------------------------------------------
    # Read the archive back. A zip that builds but cannot be imported is the
    # failure this script exists to prevent, so the check is on the artefact
    # rather than on the intent.
    $verify = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $entries = $verify.Entries | ForEach-Object { $_.FullName }

        $bad = @($entries | Where-Object { $_ -match '\\' })
        if ($bad) {
            throw ("Archive contains backslash separators: " +
                   ($bad -join ', '))
        }

        $expected = "$SkillName/SKILL.md"
        if ($entries -notcontains $expected) {
            throw ("Archive is missing $expected; found: " +
                   ($entries -join ', '))
        }

        Write-Ok "$($entries.Count) entries, all forward-slashed"
        if (-not $Quiet) {
            foreach ($entry in ($entries | Sort-Object)) {
                Write-Host "         $entry" -ForegroundColor DarkGray
            }
        }
    }
    finally {
        $verify.Dispose()
    }

    $info = Get-Item $zipPath
    Write-Host ("Built {0} ({1:N0} KB, {2} files)" -f `
        $info.FullName.Substring($root.Length + 1), ($info.Length / 1KB), $files.Count) `
        -ForegroundColor Green
    if (-not $Quiet) {
        Write-Host "Import it in Claude Desktop under Settings > Skills." `
            -ForegroundColor DarkGray
    }
}
catch {
    Write-Host "FAILED: $_" -ForegroundColor Red
    exit 1
}
