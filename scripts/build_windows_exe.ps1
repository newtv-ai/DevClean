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
$executable = Join-Path $dist "DevClean.exe"
$licenseDirectory = Join-Path $dist "licenses"
$artifactsFull = [IO.Path]::GetFullPath($artifacts)
$distFull = [IO.Path]::GetFullPath($dist)
if (
    -not [IO.Path]::GetDirectoryName($distFull).Equals(
        $artifactsFull,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "Windows EXE dist must be a direct child of its dedicated artifacts directory"
}

Push-Location $root
try {
    & uv sync --frozen --python $Python
    if ($LASTEXITCODE -ne 0) { throw "locked build environment sync failed" }

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
        --collect-data devclean `
        --distpath $dist `
        --workpath $work `
        --specpath $spec `
        $entry
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }

    if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) {
        throw "PyInstaller did not produce DevClean.exe"
    }
    $maximumBytes = $MaximumMegabytes * 1MB
    $size = (Get-Item -LiteralPath $executable).Length
    if ($size -gt $maximumBytes) {
        throw "DevClean.exe is $size bytes, exceeding the $MaximumMegabytes MB product limit"
    }

    # DevClean.exe is built --windowed, so it runs in the GUI subsystem and the
    # call operator returns without waiting and without setting $LASTEXITCODE.
    # Reading $LASTEXITCODE here would observe the previous native command's 0
    # and pass unconditionally, so wait for the process and read its real code.
    $smoke = Start-Process -FilePath $executable -ArgumentList "--ui-smoke" -PassThru -Wait
    if ($null -eq $smoke.ExitCode -or $smoke.ExitCode -ne 0) {
        throw "bundled GUI construction smoke failed with exit code $($smoke.ExitCode)"
    }
    # The first GUI launch intentionally materializes the three editable rule
    # files beside the packaged EXE. The smoke launch is only a build check, so
    # remove that build-generated state before validating the public payload.
    $smokeDataDirectory = [IO.Path]::GetFullPath(
        (Join-Path $distFull "DevClean-data")
    )
    if (
        -not [IO.Path]::GetDirectoryName($smokeDataDirectory).Equals(
            $distFull,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "GUI smoke data directory escaped the Windows EXE dist directory"
    }
    if (Test-Path -LiteralPath $smokeDataDirectory) {
        Remove-Item -LiteralPath $smokeDataDirectory -Recurse -Force
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
