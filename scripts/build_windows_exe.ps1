<#
.SYNOPSIS
Build the self-contained Windows executable distributed to end users.

.DESCRIPTION
The resulting DevClean.exe embeds its Python runtime and does not require an
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
$entry = Join-Path $root "scripts\devclean_gui_entry.py"
$defaultRuleDirectory = Join-Path $root "src\devclean\config"
$defaultRuleFiles = @(
    "scan-rules.json"
    "delete-rules.json"
    "keep-rules.json"
)
$executable = Join-Path $dist "DevClean.exe"
$licenseDirectory = Join-Path $dist "licenses"
$smokeData = Join-Path $artifacts "ui-smoke-data"
$artifactsFull = [IO.Path]::GetFullPath($artifacts)
$distFull = [IO.Path]::GetFullPath($dist)
$smokeDataFull = [IO.Path]::GetFullPath($smokeData)
if (
    -not [IO.Path]::GetDirectoryName($distFull).Equals(
        $artifactsFull,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "Windows EXE dist must be a direct child of its dedicated artifacts directory"
}
if (
    -not [IO.Path]::GetDirectoryName($smokeDataFull).Equals(
        $artifactsFull,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "GUI smoke data must be a direct child of its dedicated artifacts directory"
}

Push-Location $root
try {
    & uv sync --frozen --python $Python
    if ($LASTEXITCODE -ne 0) { throw "locked build environment sync failed" }

    foreach ($ruleName in $defaultRuleFiles) {
        $rulePath = Join-Path $defaultRuleDirectory $ruleName
        if (-not (Test-Path -LiteralPath $rulePath -PathType Leaf)) {
            throw "required default rule file is missing: $rulePath"
        }
    }

    # Construct the real Tk application from source before packaging. Running
    # the final --uac-admin EXE as a build smoke test is not reliable: UAC can
    # detach it from the caller, drop the argument handoff, or block forever in
    # a non-interactive CI desktop. PyInstaller performs the packaged import
    # analysis below; this smoke verifies the application's own construction.
    $previousPythonPath = $env:PYTHONPATH
    $previousDataDirectory = $env:DEVCLEAN_DATA_DIR
    try {
        $env:PYTHONPATH = Join-Path $root "src"
        $env:DEVCLEAN_DATA_DIR = $smokeDataFull
        & uv run --frozen --python $Python python $entry --ui-smoke
        if ($LASTEXITCODE -ne 0) {
            throw "GUI construction smoke failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $env:PYTHONPATH = $previousPythonPath
        $env:DEVCLEAN_DATA_DIR = $previousDataDirectory
        if (Test-Path -LiteralPath $smokeDataFull) {
            Remove-Item -LiteralPath $smokeDataFull -Recurse -Force
        }
    }

    # dist is generated output. Recreate it so withdrawn or renamed executables
    # can never be carried into a new release payload by an incremental build.
    if (Test-Path -LiteralPath $distFull) {
        Remove-Item -LiteralPath $distFull -Recurse -Force
    }
    [void](New-Item -ItemType Directory -Path $distFull)

    & uv run --frozen --python $Python pyinstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --uac-admin `
        --name DevClean `
        --paths src `
        --hidden-import devclean.config `
        --add-data "$defaultRuleDirectory\scan-rules.json;devclean\config" `
        --add-data "$defaultRuleDirectory\delete-rules.json;devclean\config" `
        --add-data "$defaultRuleDirectory\keep-rules.json;devclean\config" `
        --distpath $dist `
        --workpath $work `
        --specpath $spec `
        $entry
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }

    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "PyInstaller did not produce DevClean.exe"
    }
    $archiveListing = @(
        & uv run --frozen --python $Python python -m `
            PyInstaller.utils.cliutils.archive_viewer -r -l $executable
    )
    if ($LASTEXITCODE -ne 0) {
        throw "could not inspect the packaged EXE archive"
    }
    $archiveText = $archiveListing -join "`n"
    if (-not $archiveText.Contains("'devclean.config'")) {
        throw "packaged EXE is missing the devclean.config resource package"
    }
    foreach ($ruleName in $defaultRuleFiles) {
        $packagedRule = "devclean\config\$ruleName"
        # archive_viewer prints member names as Python repr strings, so each
        # path separator appears as two literal backslashes in its output.
        $archiveEntry = $packagedRule.Replace("\", "\\")
        if (-not $archiveText.Contains($archiveEntry)) {
            throw "packaged EXE is missing default rule: $packagedRule"
        }
    }
    $maximumBytes = $MaximumMegabytes * 1MB
    $size = (Get-Item -LiteralPath $executable).Length
    if ($size -gt $maximumBytes) {
        throw "DevClean.exe is $size bytes, exceeding the $MaximumMegabytes MB product limit"
    }

    $pythonBasePrefix = & uv run --frozen --python $Python python -c `
        "import sys; print(sys.base_prefix)"
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($pythonBasePrefix)) {
        throw "could not locate the bundled CPython runtime"
    }
    $pyinstallerLicenseMatches = @(
        Get-ChildItem -Path (
            Join-Path $root ".venv\Lib\site-packages\pyinstaller-*.dist-info\licenses\COPYING.txt"
        ) -File
    )
    if ($pyinstallerLicenseMatches.Count -ne 1) {
        throw "could not uniquely locate the locked PyInstaller license text"
    }
    $licenseSources = [ordered]@{
        "DevClean-GPL-3.0.txt" = (Join-Path $root "LICENSE")
        "CPython-LICENSE.txt" = (Join-Path $pythonBasePrefix "LICENSE.txt")
        "Tcl-Tk-license.terms" = (
            Join-Path $pythonBasePrefix "tcl\tk8.6\license.terms"
        )
        "PyInstaller-COPYING.txt" = $pyinstallerLicenseMatches[0].FullName
    }
    foreach ($source in $licenseSources.Values) {
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "required bundled-runtime license text is missing: $source"
        }
    }
    [void](New-Item -ItemType Directory -Path $licenseDirectory -Force)
    foreach ($notice in $licenseSources.GetEnumerator()) {
        Copy-Item -LiteralPath $notice.Value -Destination (Join-Path $licenseDirectory $notice.Key) -Force
    }

    # The public payload is deliberately tiny. Reject scan databases, AI review
    # exports, logs, or any other machine-local file accidentally copied here.
    $allowedPayloadFiles = @(
        "DevClean.exe"
        "licenses\CPython-LICENSE.txt"
        "licenses\DevClean-GPL-3.0.txt"
        "licenses\PyInstaller-COPYING.txt"
        "licenses\Tcl-Tk-license.terms"
    ) | Sort-Object
    $actualPayloadFiles = @(
        Get-ChildItem -LiteralPath $distFull -File -Recurse |
            ForEach-Object {
                $_.FullName.Substring($distFull.Length + 1)
            }
    ) | Sort-Object
    $payloadDifference = @(
        Compare-Object -ReferenceObject $allowedPayloadFiles -DifferenceObject $actualPayloadFiles
    )
    if ($payloadDifference.Count -ne 0) {
        throw "Windows EXE payload does not match the public release file allowlist"
    }

    $hash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    [pscustomobject]@{
        executable = $executable
        bytes = $size
        sha256 = $hash
        maximum_bytes = $maximumBytes
        python_required_by_user = $false
        license_directory = $licenseDirectory
        license_files = @(
            Get-ChildItem -LiteralPath $licenseDirectory -File |
                Sort-Object Name |
                Select-Object -ExpandProperty Name
        )
    } | ConvertTo-Json -Depth 3
}
finally {
    Pop-Location
}
