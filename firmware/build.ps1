<#
.SYNOPSIS
  Compile (and optionally upload) the Inkplate weather firmware with a project-local Arduino CLI.

.DESCRIPTION
  Everything lives under <repo>\.tools (gitignored): arduino-cli.exe, the board package, the library
  and download caches. Nothing is installed system-wide. Run with -Setup once to download it all.

.EXAMPLE
  .\firmware\build.ps1 -Setup                 # first time: fetch arduino-cli, board package, library
  .\firmware\build.ps1                        # compile for Soldered Inkplate 10
  .\firmware\build.ps1 -Board eradionica      # compile for the older e-radionica Inkplate 10
  .\firmware\build.ps1 -Upload -Port COM5     # compile and flash (only when you mean to!)
  .\firmware\build.ps1 -Export                # compile, then write firmware\dist\inkplate_weather-<ver>.bin + manifest.json
#>
param(
    [switch]$Setup,
    [switch]$Upload,
    # After compiling, copy the app image to firmware\dist\inkplate_weather-<FW_VERSION>.bin and write manifest.json
    # next to it (publish both to the server's cache\firmware\ folder for over-the-air updates).
    [switch]$Export,
    [string]$Port = "",
    [ValidateSet("soldered", "eradionica")]
    [string]$Board = "soldered",
    # Where .tools lives. Defaults to the repo root that contains this script.
    [string]$ToolsDir = ""
)

# Not "Stop": arduino-cli prints compiler warnings on stderr, which PowerShell 5.1 would turn into
# terminating errors when output is redirected. Failures are detected from $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"

# Pinned versions (see firmware/README.md).
$CliVersion = "1.5.1"
$CliSha256 = "fabe42e0eb04d00e776a66178299ff95a46c623dbc260f997e58fd514853dd40"
$CoreVersion = "4.0.0"
$LibVersion = "11.2.1"
$BoardIndexUrl = "https://github.com/SolderedElectronics/Inkplate-Board-Definitions-for-Arduino-IDE/raw/refs/heads/main/package_Inkplate_Boards_index.json"

# PartitionScheme=no_fs: two 2 MB app slots, which over-the-air updates need. The board's own default, huge_app,
# has ONE app slot and no OTA. The first flash over USB must use this scheme (it rewrites the partition
# table); later builds can then be pushed over the air.
$Partitions = "PartitionScheme=no_fs"
$BoardId = if ($Board -eq "soldered") { "soldered-inkplate-boards:esp32:Inkplate10V2" } else { "soldered-inkplate-boards:esp32:Inkplate10" }
$Fqbn = "${BoardId}:$Partitions"

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ToolsDir) { $ToolsDir = Join-Path $RepoRoot ".tools" }
$Sketch = Join-Path $PSScriptRoot "inkplate_weather"
$Cli = Join-Path $ToolsDir "arduino-cli\arduino-cli.exe"
$CliConfig = Join-Path $ToolsDir "arduino-cli.yaml"

function Write-CliConfig {
    $t = $ToolsDir -replace '\\', '/'
    @"
board_manager:
  additional_urls:
    - $BoardIndexUrl
directories:
  data: $t/arduino-data
  downloads: $t/arduino-downloads
  user: $t/arduino-user
build_cache:
  path: $t/arduino-build-cache
network:
  connection_timeout: 600s
"@ | Set-Content -Path $CliConfig -Encoding ascii
}

if ($Setup) {
    New-Item -ItemType Directory -Force (Join-Path $ToolsDir "arduino-cli") | Out-Null
    if (-not (Test-Path $Cli)) {
        $zip = Join-Path $ToolsDir "arduino-cli_$($CliVersion)_Windows_64bit.zip"
        $url = "https://github.com/arduino/arduino-cli/releases/download/v$CliVersion/arduino-cli_$($CliVersion)_Windows_64bit.zip"
        Write-Host "Downloading $url"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -ErrorAction Stop
        $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($hash -ne $CliSha256) { throw "arduino-cli checksum mismatch: $hash" }
        Expand-Archive -Path $zip -DestinationPath (Join-Path $ToolsDir "arduino-cli") -Force -ErrorAction Stop
        Remove-Item $zip
    }
    Write-CliConfig
    & $Cli --config-file $CliConfig core update-index
    if ($LASTEXITCODE -ne 0) { throw "arduino-cli core update-index failed" }
    & $Cli --config-file $CliConfig core install "soldered-inkplate-boards:esp32@$CoreVersion"
    if ($LASTEXITCODE -ne 0) { throw "arduino-cli core install failed" }
    & $Cli --config-file $CliConfig lib update-index
    if ($LASTEXITCODE -ne 0) { throw "arduino-cli lib update-index failed" }
    & $Cli --config-file $CliConfig lib install "InkplateLibrary@$LibVersion"
    if ($LASTEXITCODE -ne 0) { throw "arduino-cli lib install failed" }
}

if (-not (Test-Path $Cli)) { throw "arduino-cli not found at $Cli. Run: .\firmware\build.ps1 -Setup" }
Write-CliConfig # always rewrite, so every run uses only the local directories

if (-not (Test-Path (Join-Path $Sketch "config.h"))) {
    throw "Missing $Sketch\config.h. Copy config.example.h to config.h and fill in Wi-Fi and server settings."
}

$compileArgs = @("--config-file", $CliConfig, "compile", "--fqbn", $Fqbn, "--warnings", "default")
$DistDir = Join-Path $PSScriptRoot "dist"
$BuildOut = Join-Path $DistDir "build"
if ($Export) { $compileArgs += @("--output-dir", $BuildOut) }
& $Cli @compileArgs $Sketch
if ($LASTEXITCODE -ne 0) { throw "Compile failed" }

if ($Export) {
    $ver = [regex]::Match((Get-Content (Join-Path $Sketch "version.h") -Raw), '#define\s+FW_VERSION\s+(\d+)').Groups[1].Value
    if (-not $ver) { throw "Couldn't read FW_VERSION from version.h" }
    $built = Join-Path $BuildOut "inkplate_weather.ino.bin"
    if (-not (Test-Path $built)) { throw "Expected build output $built not found" }
    $name = "inkplate_weather-$ver.bin"
    $dest = Join-Path $DistDir $name
    Copy-Item $built $dest -Force
    $md5 = (Get-FileHash $dest -Algorithm MD5).Hash.ToLower()
    $sha = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
    $size = (Get-Item $dest).Length
    $builtAt = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $manifest = @(
        "{",
        "  `"version`": $ver,",
        "  `"file`": `"$name`",",
        "  `"md5`": `"$md5`",",
        "  `"sha256`": `"$sha`",",
        "  `"size`": $size,",
        "  `"fqbn`": `"$Fqbn`",",
        "  `"built_at`": `"$builtAt`"",
        "}"
    ) -join "`n"
    [System.IO.File]::WriteAllText((Join-Path $DistDir "manifest.json"), $manifest + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Exported $dest ($size bytes, md5 $md5) and manifest.json"
    Write-Host "Publish: scp firmware/dist/$name firmware/dist/manifest.json pi@<server>:~/inkplate-weather/cache/firmware/"
}

if ($Upload) {
    if (-not $Port) { throw "Pass -Port COMx (see firmware/README.md, 'Find the COM port')." }
    & $Cli --config-file $CliConfig upload --fqbn $Fqbn --port $Port $Sketch
    if ($LASTEXITCODE -ne 0) { throw "Upload failed" }
}
