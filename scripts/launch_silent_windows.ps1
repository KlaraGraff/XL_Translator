param(
    [Parameter(Mandatory = $true)]
    [string]$PythonwPath,

    [Parameter(Mandatory = $true)]
    [string]$LauncherScript,

    [Parameter(Mandatory = $true)]
    [string]$WorkingDirectory
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PythonwPath)) {
    Write-Error "Missing pythonw interpreter: $PythonwPath"
    exit 1
}

if (-not (Test-Path -LiteralPath $LauncherScript)) {
    Write-Error "Missing launcher script: $LauncherScript"
    exit 1
}

if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    Write-Error "Missing working directory: $WorkingDirectory"
    exit 1
}

try {
    $resolvedWorkingDirectory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
    $quotedLauncherScript = '"' + $LauncherScript.Replace('"', '""') + '"'
    $argumentLine = "$quotedLauncherScript --silent"

    Start-Process `
        -FilePath $PythonwPath `
        -ArgumentList $argumentLine `
        -WorkingDirectory $resolvedWorkingDirectory `
        -WindowStyle Hidden | Out-Null
    exit 0
}
catch {
    Write-Error $_.Exception.Message
    exit 1
}
