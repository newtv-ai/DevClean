[CmdletBinding()]
param(
    [Parameter()]
    [string]$Python = "3.13",

    [Parameter()]
    [string]$SourceRevision = "",

    [Parameter()]
    [string]$EvidenceOutput = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-GitProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $previousErrorAction = $ErrorActionPreference
    $output = $null
    $exitCode = -1
    try {
        # Windows PowerShell can promote native stderr to a terminating ErrorRecord
        # under Stop even when the caller intends to inspect LASTEXITCODE. Git metadata
        # is optional here, so isolate only this probe from the build's fail-fast policy.
        $ErrorActionPreference = "SilentlyContinue"
        $output = (& git @Arguments 2>$null)
        $exitCode = $LASTEXITCODE
    }
    catch {
        $output = $null
        $exitCode = -1
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    [PSCustomObject]@{
        ExitCode = $exitCode
        Output = (($output | Out-String).Trim())
    }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$artifactsDir = [IO.Path]::GetFullPath((Join-Path $root "artifacts"))
$releaseDir = Join-Path $root "artifacts\release"
$runtimeDir = Join-Path $root "artifacts\runtime-venv"
$runtimePython = Join-Path $runtimeDir "Scripts\python.exe"
$sbom = Join-Path $releaseDir "reclaimer.cdx.json"
$checksumManifest = Join-Path $releaseDir "SHA256SUMS.txt"
if ([string]::IsNullOrWhiteSpace($EvidenceOutput)) {
    $EvidenceOutput = Join-Path $root "artifacts\release-validation.json"
}
elseif (-not [IO.Path]::IsPathRooted($EvidenceOutput)) {
    $EvidenceOutput = Join-Path $root $EvidenceOutput
}
$evidencePath = [IO.Path]::GetFullPath($EvidenceOutput)
$evidenceDirectory = [IO.Path]::GetDirectoryName($evidencePath)
$artifactsPrefix = $artifactsDir.TrimEnd([IO.Path]::DirectorySeparatorChar) + `
    [IO.Path]::DirectorySeparatorChar
$releasePrefix = [IO.Path]::GetFullPath($releaseDir).TrimEnd(
    [IO.Path]::DirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar
if (-not $evidencePath.StartsWith(
        $artifactsPrefix,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "EvidenceOutput must stay beneath the repository artifacts directory"
}
if ($evidencePath.StartsWith($releasePrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "EvidenceOutput must stay outside the validated release payload directory"
}
if (-not $evidenceDirectory.Equals(
        $artifactsDir,
        [StringComparison]::OrdinalIgnoreCase
    )) {
    throw "EvidenceOutput must be a direct child of the repository artifacts directory"
}

Push-Location $root
try {
    if ([string]::IsNullOrWhiteSpace($SourceRevision)) {
        $revisionProbe = Invoke-GitProbe -Arguments @("rev-parse", "HEAD")
        $SourceRevision = $revisionProbe.Output
        if (
            $revisionProbe.ExitCode -ne 0 -or
            [string]::IsNullOrWhiteSpace($SourceRevision)
        ) {
            $SourceRevision = "WORKTREE_UNCOMMITTED"
        }
    }
    if ($SourceRevision -notmatch "^[A-Za-z0-9][A-Za-z0-9._+-]{6,127}$") {
        throw "SourceRevision does not match the bounded evidence contract"
    }
    if ([string]::IsNullOrWhiteSpace($env:SOURCE_DATE_EPOCH)) {
        $epochProbe = Invoke-GitProbe -Arguments @("log", "-1", "--pretty=%ct")
        $epoch = $epochProbe.Output
        if ($epochProbe.ExitCode -ne 0 -or $epoch -notmatch "^[0-9]+$") {
            throw "SOURCE_DATE_EPOCH is unset and no Git commit timestamp is available"
        }
        $env:SOURCE_DATE_EPOCH = $epoch
    }
    if ($env:SOURCE_DATE_EPOCH -notmatch "^[0-9]+$") {
        throw "SOURCE_DATE_EPOCH must be a non-negative integer"
    }

    & uv lock --check
    if ($LASTEXITCODE -ne 0) { throw "uv.lock is not current for pyproject.toml" }
    & uv sync --frozen
    if ($LASTEXITCODE -ne 0) { throw "locked development environment sync failed" }
    & uv run --frozen python scripts/validate_schemas.py
    if ($LASTEXITCODE -ne 0) { throw "checked-in JSON Schema validation failed" }

    & uv build --wheel --clear --no-create-gitignore --out-dir $releaseDir `
        --python $Python --no-build-isolation
    if ($LASTEXITCODE -ne 0) { throw "uv build failed with exit code $LASTEXITCODE" }

    $wheels = @(Get-ChildItem -LiteralPath $releaseDir -Filter "*.whl" -File)
    if ($wheels.Count -ne 1) {
        throw "wheel build must produce exactly one artifact; found $($wheels.Count)"
    }
    $wheel = $wheels[0]

    & uv venv --clear --python $Python $runtimeDir
    if ($LASTEXITCODE -ne 0) { throw "runtime virtual environment creation failed" }
    & uv pip install --python $runtimePython --no-deps --no-index $wheel.FullName
    if ($LASTEXITCODE -ne 0) { throw "wheel installation into clean runtime failed" }
    & uv pip check --python $runtimePython
    if ($LASTEXITCODE -ne 0) { throw "installed wheel failed dependency validation" }
    & $runtimePython -c "import importlib.metadata as m; names=sorted(d.metadata['Name'].lower() for d in m.distributions()); assert names == ['reclaimer'], names; import reclaimer; assert reclaimer.__version__ == m.version('reclaimer')"
    if ($LASTEXITCODE -ne 0) { throw "clean runtime smoke check failed" }
    $projectVersion = (& $runtimePython -c "import importlib.metadata as m; print(m.version('reclaimer'))").Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($projectVersion)) {
        throw "unable to read installed Reclaimer version"
    }
    $cliHelp = (& $runtimePython -m reclaimer.cli.main --help 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw "installed CLI help smoke failed" }
    foreach ($requiredCommand in @("scan", "report", "plan", "recycle")) {
        if ($cliHelp -notmatch "\b$requiredCommand\b") {
            throw "installed CLI help is missing $requiredCommand"
        }
    }
    if ($cliHelp -match "(?m)^\s*(apply|execute|clean|delete)\s") {
        throw "installed CLI unexpectedly exposes an execution command"
    }

    & uv run --frozen cyclonedx-py environment $runtimeDir `
        --pyproject pyproject.toml `
        --spec-version 1.6 `
        --output-format JSON `
        --output-file $sbom `
        --output-reproducible `
        --validate
    if ($LASTEXITCODE -ne 0) { throw "CycloneDX SBOM generation or validation failed" }

    $firstWheelName = $wheel.Name
    $firstWheelHash = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash
    $firstSbomHash = (Get-FileHash -LiteralPath $sbom -Algorithm SHA256).Hash

    & uv build --wheel --clear --no-create-gitignore --out-dir $releaseDir `
        --python $Python --no-build-isolation
    if ($LASTEXITCODE -ne 0) { throw "reproducibility wheel build failed" }
    $wheels = @(Get-ChildItem -LiteralPath $releaseDir -Filter "*.whl" -File)
    if ($wheels.Count -ne 1 -or $wheels[0].Name -ne $firstWheelName) {
        throw "reproducibility build produced a different wheel filename or artifact count"
    }
    $wheel = $wheels[0]
    $secondWheelHash = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash
    if ($secondWheelHash -cne $firstWheelHash) {
        throw "wheel is not byte-for-byte reproducible for the same SOURCE_DATE_EPOCH"
    }

    & uv run --frozen cyclonedx-py environment $runtimeDir `
        --pyproject pyproject.toml `
        --spec-version 1.6 `
        --output-format JSON `
        --output-file $sbom `
        --output-reproducible `
        --validate
    if ($LASTEXITCODE -ne 0) { throw "reproducibility SBOM generation failed" }
    $secondSbomHash = (Get-FileHash -LiteralPath $sbom -Algorithm SHA256).Hash
    if ($secondSbomHash -cne $firstSbomHash) {
        throw "SBOM is not byte-for-byte reproducible for the same runtime environment"
    }

    $hashLines = foreach ($file in @($wheel.FullName, $sbom)) {
        $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $([IO.Path]::GetFileName($file))"
    }
    [IO.File]::WriteAllLines(
        $checksumManifest,
        $hashLines,
        [Text.UTF8Encoding]::new($false)
    )

    & uv run --frozen python scripts/validate_release_artifacts.py --directory $releaseDir
    if ($LASTEXITCODE -ne 0) { throw "release artifact validation failed" }

    $evidenceParent = Split-Path -Parent $evidencePath
    if (-not (Test-Path -LiteralPath $evidenceParent -PathType Container)) {
        [IO.Directory]::CreateDirectory($evidenceParent) | Out-Null
    }
    $evidence = [ordered]@{
        schema_version = "1.0.0"
        source_revision = $SourceRevision
        version = $projectVersion
        captured_at = [DateTime]::UtcNow.ToString("o")
        artifact_sha256 = $secondWheelHash.ToLowerInvariant()
        wheel_sha256 = $secondWheelHash.ToLowerInvariant()
        sbom_sha256 = $secondSbomHash.ToLowerInvariant()
        checksums_sha256 = (Get-FileHash -LiteralPath $checksumManifest -Algorithm SHA256).Hash.ToLowerInvariant()
        builder_sha256 = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant()
        validator_sha256 = (Get-FileHash -LiteralPath (Join-Path $root "scripts\validate_release_artifacts.py") -Algorithm SHA256).Hash.ToLowerInvariant()
        uv_lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $root "uv.lock") -Algorithm SHA256).Hash.ToLowerInvariant()
        clean_runtime_install = $true
        wheel_reproducible = $true
        sbom_reproducible = $true
        schemas_validated = $true
        wheel_record_validated = $true
        inventory_only_surface_validated = $true
        result = "PASS"
    }
    $evidenceJson = $evidence | ConvertTo-Json -Depth 4
    $temporaryEvidencePath = Join-Path $evidenceParent (
        "." + [IO.Path]::GetFileName($evidencePath) + ".tmp-" +
        [Guid]::NewGuid().ToString("N")
    )
    $backupEvidencePath = $null
    try {
        $stream = [IO.File]::Open(
            $temporaryEvidencePath,
            [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write,
            [IO.FileShare]::None
        )
        try {
            $writer = [IO.StreamWriter]::new(
                $stream,
                [Text.UTF8Encoding]::new($false)
            )
            try {
                $writer.Write($evidenceJson + "`n")
                $writer.Flush()
                $stream.Flush($true)
            }
            finally {
                $writer.Dispose()
            }
        }
        finally {
            $stream.Dispose()
        }
        if (Test-Path -LiteralPath $evidencePath -PathType Leaf) {
            $backupEvidencePath = Join-Path $evidenceParent (
                "." + [IO.Path]::GetFileName($evidencePath) + ".old-" +
                [Guid]::NewGuid().ToString("N")
            )
            [IO.File]::Replace(
                $temporaryEvidencePath,
                $evidencePath,
                $backupEvidencePath
            )
            [IO.File]::Delete($backupEvidencePath)
            $backupEvidencePath = $null
        }
        elseif (Test-Path -LiteralPath $evidencePath) {
            throw "EvidenceOutput exists but is not a regular file"
        }
        else {
            [IO.File]::Move($temporaryEvidencePath, $evidencePath)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporaryEvidencePath) {
            Remove-Item -LiteralPath $temporaryEvidencePath -Force
        }
        if (
            $null -ne $backupEvidencePath -and
            (Test-Path -LiteralPath $backupEvidencePath)
        ) {
            Remove-Item -LiteralPath $backupEvidencePath -Force
        }
    }
}
finally {
    Pop-Location
}
