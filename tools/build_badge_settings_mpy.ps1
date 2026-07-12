[CmdletBinding()]
param(
    [string]$MpyCross = "mpy-cross",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$sourceDirectory = Join-Path $repoRoot "badge_settings"
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "release\badge_settings"
}
$manifestPath = Join-Path (Split-Path -Parent $OutputDirectory) "badge_settings-manifest.json"

if (Test-Path -LiteralPath $MpyCross -PathType Leaf) {
    $compiler = (Resolve-Path -LiteralPath $MpyCross).Path
} else {
    $compiler = (Get-Command -Name $MpyCross -CommandType Application -ErrorAction Stop).Source
}

$versionOutput = (& $compiler --version 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0) {
    throw "mpy-cross --version failed"
}
if ($versionOutput -notmatch "MicroPython v1\.23\.0\b" -or $versionOutput -notmatch "mpy v6\.3\b") {
    throw "Expected mpy-cross v1.23.0 emitting mpy v6.3; got: $versionOutput"
}

$modules = @(
    "badge_settings_app",
    "badge_settings_model",
    "badge_settings_network",
    "badge_settings_secrets",
    "badge_settings_wled"
)
$allowedFiles = @("__init__.py", "icon.png") + ($modules | ForEach-Object { "$_.mpy" })
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$unexpected = Get-ChildItem -LiteralPath $OutputDirectory -File | Where-Object { $_.Name -notin $allowedFiles }
if ($unexpected) {
    throw "Output contains unexpected files: $($unexpected.Name -join ', ')"
}

Copy-Item -LiteralPath (Join-Path $sourceDirectory "__init__.py") -Destination (Join-Path $OutputDirectory "__init__.py") -Force
Copy-Item -LiteralPath (Join-Path $sourceDirectory "icon.png") -Destination (Join-Path $OutputDirectory "icon.png") -Force

foreach ($module in $modules) {
    $input = Join-Path $sourceDirectory "$module.py"
    $output = Join-Path $OutputDirectory "$module.mpy"
    $embeddedName = "badge_settings/$module.py"
    & $compiler -march=armv7m -s $embeddedName -o $output $input
    if ($LASTEXITCODE -ne 0) {
        throw "Compilation failed for $module.py"
    }
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

$files = [ordered]@{}
foreach ($name in @("__init__.py", "icon.png")) {
    $sourcePath = Join-Path $sourceDirectory $name
    $outputPath = Join-Path $OutputDirectory $name
    $files[$name] = [ordered]@{
        source = "badge_settings/$name"
        source_sha256 = Get-Sha256 $sourcePath
        sha256 = Get-Sha256 $outputPath
        size = (Get-Item -LiteralPath $outputPath).Length
    }
}
foreach ($module in $modules) {
    $name = "$module.mpy"
    $sourcePath = Join-Path $sourceDirectory "$module.py"
    $outputPath = Join-Path $OutputDirectory $name
    $header = [System.IO.File]::ReadAllBytes($outputPath)[0..3]
    $headerHex = ($header | ForEach-Object { $_.ToString("x2") }) -join ""
    if ($headerHex -ne "4d06001f") {
        throw "Unexpected MPY header for $name`: $headerHex"
    }
    $files[$name] = [ordered]@{
        source = "badge_settings/$module.py"
        source_sha256 = Get-Sha256 $sourcePath
        sha256 = Get-Sha256 $outputPath
        size = (Get-Item -LiteralPath $outputPath).Length
    }
}

$manifest = [ordered]@{
    schema_version = 1
    application = "badge_settings"
    target = [ordered]@{
        board = "GitHub Universe 2025 Badge (RP2350)"
        firmware = "MonaOS"
        micropython = "1.23.0"
        mpy_abi = "6.3"
        architecture = "armv7m"
        mpy_header_hex = "4d06001f"
    }
    compiler = [ordered]@{
        name = "mpy-cross"
        version = "1.23.0"
        command = "mpy-cross -march=armv7m -s badge_settings/<module>.py -o release/badge_settings/<module>.mpy badge_settings/<module>.py"
    }
    files = $files
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

Write-Host "Built $($modules.Count) compiled modules in $OutputDirectory"
Write-Host "Manifest: $manifestPath"
