param(
    [switch]$Fix
)

$ErrorActionPreference = "Stop"

$venvPythonCandidates = @(
    (Join-Path $PSScriptRoot ".venv/bin/python3"),
    (Join-Path $PSScriptRoot ".venv/bin/python")
)
$python = $venvPythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$targets = @(
    "app.py",
    "config.py",
    "settings.py",
    "core",
    "ui",
    "engines"
)

if (-not $python) {
    Write-Error "Project venv Python not found under .venv/bin/python3"
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
