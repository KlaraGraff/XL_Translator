param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$DistDir = Join-Path $Root "dist"
$SpecPath = Join-Path $Root "packaging/windows/app_windows.spec"
$InstallerScript = Join-Path $Root "packaging/windows/app_windows.iss"
$ConstraintsPath = Join-Path $Root "constraints-release-py311.txt"

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

$PythonVersion = (& $Python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($PythonVersion -ne "3.11") {
    throw "Python 3.11 is required for release builds; got $PythonVersion from $Python."
}

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$env:PYINSTALLER_CONFIG_DIR = Join-Path $Root ".runtime/pyinstaller-config"
New-Item -ItemType Directory -Force -Path $env:PYINSTALLER_CONFIG_DIR | Out-Null

Invoke-Step "Verify build dependencies" {
    & $Python -c "import PyInstaller, PIL; from PySide6 import QtWidgets; print('ok')"
}
Invoke-Step "Verify release dependency versions" {
    & $Python "scripts/verify_release_dependencies.py" --constraints $ConstraintsPath
}

$Version = (& $Python -c "import app_meta; print(app_meta.APP_VERSION)").Trim()
if (-not $Version) {
    throw "APP_VERSION could not be resolved."
}
Invoke-Step "Verify changelog version" {
    & $Python "scripts/check_changelog_version.py" --version $Version
}
$AppName = (& $Python -c "import app_meta; print(app_meta.APP_NAME)").Trim()
$PackageName = (& $Python -c "import app_meta; print(app_meta.WINDOWS_PACKAGE_NAME)").Trim()
$ExeName = (& $Python -c "import app_meta; print(app_meta.WINDOWS_EXE_NAME)").Trim()
$SetupBaseName = (& $Python -c "import app_meta; print(app_meta.WINDOWS_SETUP_BASENAME)").Trim()
if (-not $AppName -or -not $PackageName -or -not $ExeName -or -not $SetupBaseName) {
    throw "App packaging metadata could not be resolved."
}

Invoke-Step "Prepare Windows icon" {
    & $Python "scripts/prepare_icons.py" --windows
}

$SpecBuildName = [IO.Path]::GetFileNameWithoutExtension($SpecPath)
$BuildDir = Join-Path $Root "build/$SpecBuildName"
$PackageDir = Join-Path $DistDir $PackageName
$SetupName = "$SetupBaseName.exe"
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

$ExePath = Join-Path $PackageDir $ExeName
if (-not (Test-Path $ExePath)) {
    throw "Expected executable was not produced: $ExePath"
}

Invoke-Step "Verify frozen executable startup" {
    & $Python "scripts/run_frozen_smoke.py" $ExePath --timeout 60
}

function Invoke-OptionalAuthenticodeSign {
    param([string]$Path)

    $certificateSha1 = $env:XL_TRANSLATOR_WINDOWS_SIGN_CERT_SHA1
    if (-not $certificateSha1) {
        Write-Host "[INFO] No Windows signing certificate configured; leaving $Path unsigned."
        return
    }

    $signToolCommand = if ($env:XL_TRANSLATOR_WINDOWS_SIGNTOOL) {
        Get-Command $env:XL_TRANSLATOR_WINDOWS_SIGNTOOL -ErrorAction Stop
    } else {
        Get-Command signtool.exe -ErrorAction Stop
    }
    $arguments = @(
        "sign",
        "/sha1", $certificateSha1,
        "/fd", "SHA256"
    )
    if ($env:XL_TRANSLATOR_WINDOWS_TIMESTAMP_URL) {
        $arguments += @(
            "/tr", $env:XL_TRANSLATOR_WINDOWS_TIMESTAMP_URL,
            "/td", "SHA256"
        )
    }
    $arguments += $Path
    Invoke-Step "Sign $Path" {
        & $signToolCommand.Source @arguments
    }
    $signature = Get-AuthenticodeSignature -FilePath $Path
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        throw "Authenticode verification failed for $Path`: $($signature.StatusMessage)"
    }
}

Invoke-OptionalAuthenticodeSign -Path $ExePath

$Iscc = Resolve-Iscc
Invoke-Step "Build Windows installer" {
    & $Iscc "/DAppVersion=$Version" "/DAppName=$AppName" "/DAppExeName=$ExeName" "/DWindowsPackageName=$PackageName" "/DOutputBaseName=$SetupBaseName" $InstallerScript
}

if (-not (Test-Path $SetupPath)) {
    throw "Expected installer was not produced: $SetupPath"
}

Invoke-OptionalAuthenticodeSign -Path $SetupPath

$Hash = (Get-FileHash -Algorithm SHA256 $SetupPath).Hash.ToLowerInvariant()
"$Hash  $SetupName" | Set-Content -Encoding ascii $ChecksumPath

if ($env:GITHUB_ENV) {
    "WINDOWS_SETUP=dist/$SetupName" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    "WINDOWS_SETUP_SHA256=dist/$SetupName.sha256" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
}

Write-Host "[INFO] Windows installer: $SetupPath"
Write-Host "[INFO] SHA256: $ChecksumPath"
