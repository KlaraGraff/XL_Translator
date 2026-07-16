param(
    [switch]$Fix
)

$ErrorActionPreference = "Stop"

$venvPythonCandidates = @(
    (Join-Path $PSScriptRoot ".venv/Scripts/python.exe"),
    (Join-Path $PSScriptRoot ".venv/bin/python3"),
    (Join-Path $PSScriptRoot ".venv/bin/python")
)
$python = $venvPythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
$targets = @(
    "config.py",
    "settings.py",
    "api",
    "core",
    "native_app",
    "engines",
    "scripts",
    "tests"
)

if (-not $python) {
    Write-Error "Python not found. Expected .venv or a python command on PATH."
    exit 1
}

if ($Fix) {
    & $python -m ruff check @targets --fix
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

& $python -m ruff check @targets
exit $LASTEXITCODE
