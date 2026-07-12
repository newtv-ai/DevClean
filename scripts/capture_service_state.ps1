<#
.SYNOPSIS
Captures a minimal Docker/Ollama service and process snapshot for G2 evidence.

.DESCRIPTION
This script performs only Get-Service/Get-Process queries and writes one new JSON
evidence file. It never starts, stops, installs, or configures a service. Run it
before starting the ProcMon capture and again after stopping the capture.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("before", "after")]
    [string]$Label,

    [Parameter(Mandatory = $true)]
    [string]$Output
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$outputPath = [System.IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputPath) {
    throw "Refusing to overwrite existing evidence: $outputPath"
}
$parent = Split-Path -Parent $outputPath
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    throw "Evidence parent directory does not exist: $parent"
}

$serviceNames = @("com.docker.service", "docker", "ollama")
$services = @(
    foreach ($name in $serviceNames) {
        $service = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($null -ne $service) {
            [ordered]@{
                name = $service.Name
                status = $service.Status.ToString()
            }
        }
    }
) | Sort-Object -Property name

$processNames = @("Docker Desktop", "com.docker.backend", "dockerd", "ollama", "ollama app")
$processes = @(
    foreach ($name in $processNames) {
        $matches = @(Get-Process -Name $name -ErrorAction SilentlyContinue)
        if ($matches.Count -gt 0) {
            [ordered]@{
                name = $name
                process_ids = @($matches.Id | Sort-Object)
            }
        }
    }
) | Sort-Object -Property name

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$processElevated = $principal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
$collector = Get-Item -LiteralPath $PSCommandPath
$collectorSha256 = (Get-FileHash -LiteralPath $collector.FullName -Algorithm SHA256).Hash.ToLowerInvariant()

$payload = [ordered]@{
    schema_version = "1.0.0"
    captured_at = (Get-Date).ToUniversalTime().ToString("o")
    label = $Label
    process_elevated = $processElevated
    collector_sha256 = $collectorSha256
    collector_bytes = $collector.Length
    services = @($services)
    processes = @($processes)
}
$json = $payload | ConvertTo-Json -Depth 6
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($outputPath, $json + "`n", $utf8WithoutBom)

Write-Output $outputPath
