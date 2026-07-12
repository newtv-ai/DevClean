<#
.SYNOPSIS
Build the self-contained Windows executable distributed to end users.

.DESCRIPTION
The resulting Reclaimer.exe embeds its Python runtime and does not require an
installed Python interpreter, uv, a virtual environment, or a persistent scan
database. Build products stay under artifacts/ and are ignored by Git.
#>

[CmdletBinding()]
param(
    [Parameter()]
    [string]$Python = "3.13",

    [Parameter()]
    [ValidateRange(1, 100)]
    [int]$MaximumMegabytes = 50
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$artifacts = Join-Path $root "artifacts\windows-exe"
$work = Join-Path $artifacts "work"
$spec = Join-Path $artifacts "spec"
$dist = Join-Path $artifacts "dist"
$entry = Join-Path $root "scripts\reclaimer_gui_entry.py"
$executable = Join-Path $dist "Reclaimer.exe"

Push-Location $root
try {
    & uv sync --frozen --python $Python
    if ($LASTEXITCODE -ne 0) { throw "locked build environment sync failed" }

    & uv run --frozen --python $Python pyinstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name Reclaimer `
        --paths src `
        --distpath $dist `
        --workpath $work `
        --specpath $spec `
        $entry
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }

    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "PyInstaller did not produce Reclaimer.exe"
    }
    $maximumBytes = $MaximumMegabytes * 1MB
    $size = (Get-Item -LiteralPath $executable).Length
    if ($size -gt $maximumBytes) {
        throw "Reclaimer.exe is $size bytes, exceeding the $MaximumMegabytes MB product limit"
    }

    & $executable --smoke
    if ($LASTEXITCODE -ne 0) { throw "bundled GUI executable smoke failed" }

    $hash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    [pscustomobject]@{
        executable = $executable
        bytes = $size
        sha256 = $hash
        maximum_bytes = $maximumBytes
        python_required_by_user = $false
    } | ConvertTo-Json -Depth 3
}
finally {
    Pop-Location
}
