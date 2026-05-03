param(
    [Parameter(Mandatory = $true)]
    [string]$TaskSlug,

    [Parameter(Mandatory = $true)]
    [string]$ScriptPath,

    [string[]]$ScriptArgs = @()
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$pythonCandidates = @(
    (Join-Path $projectRoot ".venv/bin/python3"),
    (Join-Path $projectRoot ".venv/bin/python")
)
$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $python) {
    Write-Error "Project venv Python not found under .venv/bin/python3"
    exit 1
}

if ([System.IO.Path]::IsPathRooted($ScriptPath)) {
    $resolvedScriptPath = $ScriptPath
}
else {
    $resolvedScriptPath = Join-Path $projectRoot $ScriptPath
}

if (-not (Test-Path $resolvedScriptPath)) {
    Write-Error "Self-test script not found: $resolvedScriptPath"
    exit 1
}

$runRoot = Join-Path $projectRoot ".runtime\self-tests\$TaskSlug"
$homeDir = Join-Path $runRoot "home"
$tempDir = Join-Path $runRoot "temp"
$artifactsDir = Join-Path $runRoot "artifacts"

New-Item -ItemType Directory -Force -Path $runRoot, $homeDir, $tempDir, $artifactsDir | Out-Null

$env:HOME = $homeDir
$env:USERPROFILE = $homeDir
$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:PRODUCT_TRANSLATE_SELF_TEST_ROOT = $runRoot
$env:PRODUCT_TRANSLATE_SELF_TEST_ARTIFACTS = $artifactsDir
if ([string]::IsNullOrWhiteSpace($env:PYTHONPATH)) {
    $env:PYTHONPATH = $projectRoot
}
else {
    $env:PYTHONPATH = "$projectRoot$([System.IO.Path]::PathSeparator)$($env:PYTHONPATH)"
}

$exitCode = 0

Push-Location $projectRoot
try {
    & $python $resolvedScriptPath @ScriptArgs
    $exitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}

exit $exitCode
