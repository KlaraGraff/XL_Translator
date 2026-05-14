param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistDir = Join-Path $Root "dist"
$SpecPath = Join-Path $Root "packaging/windows/XL_Translator_Windows.spec"
$InstallerScript = Join-Path $Root "packaging/windows/XL_Translator_Windows.iss"

function Resolve-Python {
    param([string]$ExplicitPython)

    $candidates = @()
    if ($ExplicitPython) {
        $candidates += $ExplicitPython
    }
    $candidates += @(
        (Join-Path $Root ".venv/Scripts/python.exe"),
        (Join-Path $Root ".venv/bin/python3"),
        (Join-Path $Root ".venv/bin/python")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }

    throw "Python was not found. Install Python 3.11 or pass -PythonExe."
}

function Resolve-Iscc {
    $command = Get-Command iscc -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $programFilesX86 = [Environment]::GetEnvironmentVariable("ProgramFiles(x86)")
    $programFiles = [Environment]::GetEnvironmentVariable("ProgramFiles")
    $candidates = @()
    if ($programFilesX86) {
        $candidates += Join-Path $programFilesX86 "Inno Setup 6/ISCC.exe"
    }
    if ($programFiles) {
        $candidates += Join-Path $programFiles "Inno Setup 6/ISCC.exe"
    }
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    throw "Inno Setup 6 was not found. Install it first, then rerun this script."
}

function Invoke-Step {
    param(
        [string]$Name,
        [scriptblock]$Action
    )

    Write-Host "[INFO] $Name"
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE"
    }
}

$Python = Resolve-Python -ExplicitPython $PythonExe
Set-Location $Root
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path $Root ".runtime/pyinstaller-config"
New-Item -ItemType Directory -Force -Path $env:PYINSTALLER_CONFIG_DIR | Out-Null

Invoke-Step "Verify build dependencies" {
    & $Python -c "import PyInstaller, PIL, webview; print('ok')"
}

$Version = (& $Python -c "import app_meta; print(app_meta.APP_VERSION)").Trim()
if (-not $Version) {
    throw "APP_VERSION could not be resolved."
}

Invoke-Step "Prepare Windows icon" {
    & $Python "scripts/prepare_icons.py" --windows
}

$BuildDir = Join-Path $Root "build/XL_Translator_Windows"
$PackageDir = Join-Path $DistDir "XL_Translator_Windows"
$SetupName = "XL_Translator_Windows_${Version}_Setup.exe"
$SetupPath = Join-Path $DistDir $SetupName
$ChecksumPath = "$SetupPath.sha256"

if (Test-Path $BuildDir) {
    Remove-Item -Recurse -Force $BuildDir
}
if (Test-Path $PackageDir) {
    Remove-Item -Recurse -Force $PackageDir
}
if (Test-Path $SetupPath) {
    Remove-Item -Force $SetupPath
}
if (Test-Path $ChecksumPath) {
    Remove-Item -Force $ChecksumPath
}

Invoke-Step "Build Windows app bundle" {
    & $Python -m PyInstaller --noconfirm $SpecPath
}

$ExePath = Join-Path $PackageDir "XL Translator.exe"
if (-not (Test-Path $ExePath)) {
    throw "Expected executable was not produced: $ExePath"
}

$Iscc = Resolve-Iscc
Invoke-Step "Build Windows installer" {
    & $Iscc "/DAppVersion=$Version" $InstallerScript
}

if (-not (Test-Path $SetupPath)) {
    throw "Expected installer was not produced: $SetupPath"
}

$Hash = (Get-FileHash -Algorithm SHA256 $SetupPath).Hash.ToLowerInvariant()
"$Hash  $SetupName" | Set-Content -Encoding ascii $ChecksumPath

if ($env:GITHUB_ENV) {
    "WINDOWS_SETUP=dist/$SetupName" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    "WINDOWS_SETUP_SHA256=dist/$SetupName.sha256" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
}

Write-Host "[INFO] Windows installer: $SetupPath"
Write-Host "[INFO] SHA256: $ChecksumPath"
