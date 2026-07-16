param(
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = if ($PythonExe) { $PythonExe } else { Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "Python was not found: $Python" }

$Version = (& $Python -c "import app_meta; print(app_meta.APP_VERSION)").Trim()
$Sidecar = Join-Path $Root "src-tauri\resources\sidecar\translator-sidecar\translator-sidecar.exe"

Set-Location $Root
& $Python "scripts\build_tauri_sidecar.py" --python $Python
if ($LASTEXITCODE -ne 0) { throw "Build Python sidecar failed." }

if ($env:XL_TRANSLATOR_WINDOWS_SIGN_CERT_SHA1) {
    $SignTool = (Get-Command signtool.exe -ErrorAction Stop).Source
    & $SignTool sign /sha1 $env:XL_TRANSLATOR_WINDOWS_SIGN_CERT_SHA1 /fd SHA256 $Sidecar
    if ($LASTEXITCODE -ne 0) { throw "Sign sidecar failed." }
}

& $Python "scripts\build_tauri_package.py" --platform windows --python $Python --skip-sidecar
if ($LASTEXITCODE -ne 0) { throw "Build Windows Tauri package failed." }

$BundleDir = Join-Path $Root "src-tauri\target\release\bundle\nsis"
$Installer = Get-ChildItem -Path $BundleDir -Filter "*.exe" | Select-Object -First 1
if (-not $Installer) { throw "Tauri NSIS installer was not produced." }

$DistDir = Join-Path $Root "dist"
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$OutputName = "Translator_Windows_$Version`_Setup.exe"
$OutputPath = Join-Path $DistDir $OutputName
Copy-Item -Force $Installer.FullName $OutputPath

if ($env:XL_TRANSLATOR_WINDOWS_SIGN_CERT_SHA1) {
    $SignTool = (Get-Command signtool.exe -ErrorAction Stop).Source
    & $SignTool sign /sha1 $env:XL_TRANSLATOR_WINDOWS_SIGN_CERT_SHA1 /fd SHA256 $OutputPath
    if ($LASTEXITCODE -ne 0) { throw "Sign installer failed." }
}

$SizeMb = [math]::Ceiling((Get-Item $OutputPath).Length / 1MB)
if ($SizeMb -gt 80) { throw "Installer is $SizeMb MB, exceeding the 80MB escalation threshold." }
$Hash = (Get-FileHash -Algorithm SHA256 $OutputPath).Hash.ToLowerInvariant()
"$Hash  $OutputName" | Set-Content -Encoding ascii "$OutputPath.sha256"
if ($env:GITHUB_ENV) {
    "WINDOWS_SETUP=dist/$OutputName" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
    "WINDOWS_SETUP_SHA256=dist/$OutputName.sha256" | Out-File -FilePath $env:GITHUB_ENV -Encoding utf8 -Append
}
Write-Host "[INFO] Windows NSIS installer ($SizeMb MB): $OutputPath"
