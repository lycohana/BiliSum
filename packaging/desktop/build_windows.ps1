[CmdletBinding()]
param(
    [switch]$SkipPrebuild
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$desktopDir = Join-Path $repoRoot "apps\desktop"
$rceditPatchScript = Join-Path $repoRoot "scripts\patch_electron_builder_rcedit.js"
$webStaticValidationScript = Join-Path $repoRoot "scripts\validate_web_static_assets.js"
$winCodeSignVersion = "2.6.0"
$winCodeSignArchiveName = "winCodeSign-$winCodeSignVersion.7z"
$winCodeSignUrl = "https://github.com/electron-userland/electron-builder-binaries/releases/download/winCodeSign-$winCodeSignVersion/$winCodeSignArchiveName"
$winCodeSignCacheDir = "$env:LOCALAPPDATA\electron-builder\Cache\winCodeSign"
$localRceditDir = Join-Path $winCodeSignCacheDir "bilisum-rcedit"

function Ensure-PythonPip {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe
    )

    & $PythonExe -m pip --version *> $null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host "pip is missing in the selected Python environment; bootstrapping with ensurepip..."
    & $PythonExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to bootstrap pip for $PythonExe"
    }

    & $PythonExe -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "pip is still unavailable after ensurepip for $PythonExe"
    }
}

function Invoke-ProcessChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory = $true)]
        [string]$FailureMessage
    )

    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -NoNewWindow `
        -Wait `
        -PassThru
    if ($process.ExitCode -ne 0) {
        throw $FailureMessage
    }
}

function Test-PythonModule {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe,
        [Parameter(Mandatory = $true)]
        [string]$ModuleName
    )

    $stdoutPath = [System.IO.Path]::GetTempFileName()
    $stderrPath = [System.IO.Path]::GetTempFileName()
    try {
        $process = Start-Process `
            -FilePath $PythonExe `
            -ArgumentList "-c `"import $ModuleName`"" `
            -NoNewWindow `
            -Wait `
            -PassThru `
            -RedirectStandardOutput $stdoutPath `
            -RedirectStandardError $stderrPath
        return $process.ExitCode -eq 0
    }
    finally {
        Remove-Item $stdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-IconPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BasePythonExe
    )

    if (Test-PythonModule -PythonExe $BasePythonExe -ModuleName "PIL") {
        return $BasePythonExe
    }

    $iconVenvDir = Join-Path $repoRoot "build\icon-python"
    $iconPython = Join-Path $iconVenvDir "Scripts\python.exe"

    if (-not (Test-Path $iconPython)) {
        Write-Host "Creating isolated Python environment for icon generation: $iconVenvDir"
        New-Item -ItemType Directory -Force -Path (Split-Path $iconVenvDir -Parent) | Out-Null
        Invoke-ProcessChecked `
            -FilePath $BasePythonExe `
            -ArgumentList @("-m", "venv", $iconVenvDir) `
            -FailureMessage "Failed to create icon generation Python environment."
    }

    Ensure-PythonPip -PythonExe $iconPython

    if (-not (Test-PythonModule -PythonExe $iconPython -ModuleName "PIL")) {
        Write-Host "Pillow is missing; installing Pillow into the isolated icon generation environment..."
        Invoke-ProcessChecked `
            -FilePath $iconPython `
            -ArgumentList @("-m", "pip", "install", "--disable-pip-version-check", "Pillow") `
            -FailureMessage "Failed to install Pillow for icon generation."
    }

    return $iconPython
}

function Ensure-LocalRcedit {
    $rceditX64 = Join-Path $localRceditDir "rcedit-x64.exe"
    $rceditIa32 = Join-Path $localRceditDir "rcedit-ia32.exe"
    if ((Test-Path $rceditX64) -and (Test-Path $rceditIa32)) {
        return $rceditX64
    }

    New-Item -ItemType Directory -Force -Path $localRceditDir | Out-Null

    $archivePath = Join-Path $winCodeSignCacheDir $winCodeSignArchiveName
    if (-not (Test-Path $archivePath)) {
        Write-Host "Downloading winCodeSign archive for local rcedit extraction..."
        & curl.exe -L --fail --retry 3 --retry-delay 2 --output $archivePath $winCodeSignUrl
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to download $winCodeSignUrl"
        }
    }

    $sevenZip = Join-Path $desktopDir "node_modules\7zip-bin\win\x64\7za.exe"
    if (-not (Test-Path $sevenZip)) {
        throw "7za.exe was not found: $sevenZip"
    }

    Write-Host "Extracting local rcedit binaries from winCodeSign archive..."
    & $sevenZip e -y $archivePath "-o$localRceditDir" "rcedit-x64.exe" "rcedit-ia32.exe" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to extract local rcedit binaries from $archivePath"
    }

    if (-not (Test-Path $rceditX64)) {
        throw "Extracted rcedit-x64.exe was not found: $rceditX64"
    }

    return $rceditX64
}

$python312 = (uv python find 3.12).Trim()
if (-not $python312) {
    throw "No Python 3.12 interpreter was found via uv."
}

Write-Host "Using Python 3.12:" $python312

# Reuse the existing tool caches; they may also be used by other projects.

# Create a dummy winCodeSign directory with a fake version file to prevent electron-builder from downloading
# This avoids the symlink creation issue on Windows
Write-Host "Creating dummy winCodeSign cache directory: $winCodeSignCacheDir"
New-Item -ItemType Directory -Force -Path $winCodeSignCacheDir | Out-Null
# Create a version file that electron-builder checks
"2.6.0" | Out-File -FilePath "$winCodeSignCacheDir\.version" -Encoding utf8
# Create darwin directory structure to satisfy electron-builder
$darwinLibDir = "$winCodeSignCacheDir\darwin\10.12\lib"
New-Item -ItemType Directory -Force -Path $darwinLibDir | Out-Null
# Create dummy dylib files (empty) to prevent symlink creation attempts
New-Item -ItemType File -Force -Path "$darwinLibDir\libcrypto.dylib" | Out-Null
New-Item -ItemType File -Force -Path "$darwinLibDir\libssl.dylib" | Out-Null

Push-Location $desktopDir
try {
    # Generate icon before building
    $iconScript = Join-Path $repoRoot "apps\desktop\build\generate_icon.py"
    if (-not (Test-Path $iconScript)) {
        throw "Icon generator script was not found: $iconScript"
    }
    Write-Host "Ensuring Pillow is available for icon generation..."
    $iconPython = Ensure-IconPython -BasePythonExe $python312
    Write-Host "Generating application icon..."
    & $iconPython $iconScript
    if ($LASTEXITCODE -ne 0) {
        throw "Application icon generation failed."
    }

    if (-not $SkipPrebuild) {
        npm run typecheck
        if ($LASTEXITCODE -ne 0) { throw "Desktop type checking failed." }
        npm run build:renderer
        if ($LASTEXITCODE -ne 0) { throw "Renderer build failed." }
        & $python312 (Join-Path $repoRoot "packaging\pyinstaller\build_onedir.py")
        if ($LASTEXITCODE -ne 0) { throw "Backend build failed." }

        $backendExe = Join-Path $repoRoot "dist\BiliSum\BiliSum.exe"
        if (-not (Test-Path $backendExe)) {
            throw "Packaged backend was not produced: $backendExe"
        }

        $runtimePython = Join-Path $repoRoot "build\pyinstaller\runtime\base\python.exe"
        if (-not (Test-Path $runtimePython)) {
            throw "Packaged managed runtime Python was not produced: $runtimePython"
        }
        & $runtimePython -m pip --version *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Packaged managed runtime pip is unavailable after relocation."
        }
    }
    else {
        Write-Host "SkipPrebuild enabled: reusing existing renderer and backend artifacts."
    }

    if (-not (Test-Path $webStaticValidationScript)) {
        throw "Web static asset validation script was not found: $webStaticValidationScript"
    }
    Write-Host "Validating web static asset references..."
    node $webStaticValidationScript
    if ($LASTEXITCODE -ne 0) {
        throw "Web static asset validation failed."
    }

    npm run build:electron
    if ($LASTEXITCODE -ne 0) { throw "Electron build failed." }
    
    if (-not (Test-Path $rceditPatchScript)) {
        throw "electron-builder patch script was not found: $rceditPatchScript"
    }

    Write-Host "Patching electron-builder to use local rcedit when available..."
    node $rceditPatchScript
    if ($LASTEXITCODE -eq 0) {
        # Patch succeeded - set up local rcedit
        $localRcedit = Ensure-LocalRcedit
        Write-Host "Using local rcedit:" $localRcedit
        $env:BILISUM_RCEDIT_PATH = $localRcedit
    }
    else {
        Write-Warning "electron-builder rcedit patch failed (likely due to version mismatch). Continuing without local rcedit optimization."
    }

    # Disable code signing on Windows to avoid symlink issues with darwin libraries
    Write-Host "Building with code signing disabled..."
    
    $env:CSC_IDENTITY_AUTO_DISCOVERY = "false"
    $env:CSC_PLATFORM = "windows"
    
    npx electron-builder --config electron-builder.config.js --win nsis --x64 --publish=never
    if ($LASTEXITCODE -ne 0) {
        throw "electron-builder packaging failed."
    }
}
finally {
    Pop-Location
}
