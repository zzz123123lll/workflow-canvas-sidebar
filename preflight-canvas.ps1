param(
  [string]$Plugin = "",
  [string]$CanvasHome = "$env:USERPROFILE\.dsh-workflow-canvas",
  [string]$Python = ""
)

$ErrorActionPreference = "Continue"
if (-not $Plugin) {
  if (Test-Path (Join-Path $PSScriptRoot "server")) { $Plugin = $PSScriptRoot }
  else {
    $candidate = Join-Path (Split-Path -Parent $PSScriptRoot) "dsh-plugins\workflow-canvas-sidebar"
    $Plugin = $candidate
  }
}

$rows = @()
function Add-Row($name, $ok, $detail) {
  $script:rows += [pscustomobject]@{ check = $name; status = $(if ($ok) { "PASS" } else { "FAIL" }); detail = $detail }
}
function Add-OptionalRow($name, $ok, $detail) {
  $script:rows += [pscustomobject]@{ check = $name; status = $(if ($ok) { "PASS" } else { "WARN" }); detail = $detail }
}

$configPath = Join-Path $Plugin "canvas-home.json"
if (-not $Python -and (Test-Path -LiteralPath $configPath)) {
  $localConfig = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
  if ($localConfig.python) { $Python = $localConfig.python }
  if (-not $PSBoundParameters.ContainsKey('CanvasHome') -and $localConfig.home) { $CanvasHome = $localConfig.home }
}
$py = if ($Python) { $Python } else { Join-Path $Plugin "runtime\python\python.exe" }
Add-Row "runtime python" (Test-Path $py) $py
if (Test-Path $py) {
  $version = (& $py --version 2>&1 | Out-String).Trim()
  Add-Row "python version" ($version -match "3\.1[0-9]") $version
  $pdf = (& $py -c "from pypdf import PdfReader; import pypdf; print(pypdf.__version__)" 2>&1 | Out-String).Trim()
  Add-Row "PDF text extraction" ($pdf -eq "6.14.2") $pdf
}
Add-Row "AI backend" $true "使用 Harness 宿主账号与子会话；无需独立 SDK 或另一份 Harness 运行时。"

$server = Join-Path $Plugin "server\canvas_editor_server.py"
Add-Row "server code" (Test-Path $server) $server
Add-Row "seed" (Test-Path (Join-Path $Plugin "seed\kb\modules.json")) (Join-Path $Plugin "seed")
Add-Row "install script" (Test-Path (Join-Path $Plugin "install.ps1")) "install.ps1"

$chromePaths = @(
  "C:\Program Files\Google\Chrome\Application\chrome.exe",
  "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
Add-OptionalRow "chrome" ([bool]$chrome) $(if ($chrome) { $chrome } else { "未安装：网页采集不可用，其他流程可用" })

$ffmpeg = Join-Path $Plugin "server\bin\ffmpeg.EXE"
if (-not (Test-Path $ffmpeg)) { $ffmpeg = Join-Path $CanvasHome "bin\ffmpeg.EXE" }
$ffmpegCmd = Get-Command ffmpeg -ErrorAction SilentlyContinue
Add-OptionalRow "ffmpeg" ((Test-Path $ffmpeg) -or [bool]$ffmpegCmd) $(if (Test-Path $ffmpeg) { $ffmpeg } elseif ($ffmpegCmd) { $ffmpegCmd.Source } else { "未安装：视频合成不可用，其他流程可用" })

$writeOK = $false
$probeFile = Join-Path $CanvasHome (".preflight-" + [guid]::NewGuid().ToString('N') + ".tmp")
try {
  [System.IO.File]::WriteAllText($probeFile, "workflow-canvas-write-check")
  $writeOK = [System.IO.File]::ReadAllText($probeFile) -eq "workflow-canvas-write-check"
} catch { $writeOK = $false }
finally { if (Test-Path -LiteralPath $probeFile) { Remove-Item -LiteralPath $probeFile -Force } }
Add-Row "data home writable" $writeOK $CanvasHome
Add-Row "PowerShell 7" ([bool](Get-Command pwsh -ErrorAction SilentlyContinue)) "Agent shell runtime"
$apiPort = 8793
$instancePath = Join-Path $CanvasHome '.runs\api-instance.json'
$apiHeaders = @{}
if (Test-Path -LiteralPath $instancePath) {
  try {
    $instance = Get-Content -LiteralPath $instancePath -Raw | ConvertFrom-Json
    if ($instance.port) { $apiPort = [int]$instance.port }
    if ($instance.token) { $apiHeaders['X-Canvas-Api-Token'] = [string]$instance.token }
  } catch { }
}
$expectedVersion = (Get-Content -LiteralPath (Join-Path $Plugin 'package.json') -Raw | ConvertFrom-Json).version
$expectedBuild = 'development'
$buildPath = Join-Path $Plugin 'build-info.json'
if (Test-Path -LiteralPath $buildPath) { $expectedBuild = (Get-Content -LiteralPath $buildPath -Raw | ConvertFrom-Json).build_id }
$port = Get-NetTCPConnection -State Listen -LocalPort $apiPort -ErrorAction SilentlyContinue
$apiOK = -not [bool]$port
$apiDetail = "空闲，启动插件时自动加载"
$apiNeedsRestart = $false
if ($port) {
  try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$apiPort/api/health" -Headers $apiHeaders -TimeoutSec 4
    $apiOK = $health.service -eq 'canvas-editor'
    $apiDetail = if ($apiOK) { "插件 API 在线，版本 $($health.version)" } else { "被其他服务占用" }
    if ($apiOK) {
      $expectedHome = [System.IO.Path]::GetFullPath($CanvasHome).TrimEnd('\', '/')
      $expectedServer = [System.IO.Path]::GetFullPath((Join-Path $Plugin 'server\canvas_editor_server.py'))
      $apiNeedsRestart = $health.version -ne $expectedVersion -or $health.build_id -ne $expectedBuild -or -not $health.data_home -or -not $health.server_file -or
        [System.IO.Path]::GetFullPath([string]$health.data_home).TrimEnd('\', '/') -ne $expectedHome -or
        [System.IO.Path]::GetFullPath([string]$health.server_file) -ne $expectedServer
      if ($apiNeedsRestart) { $apiDetail = '当前服务属于旧代码或其他数据目录；请退出旧宿主并重开，随后核对版本与数据目录' }
    }
  } catch { $apiDetail = "端口被占用，且不是可访问的插件 API" }
}
if ($apiNeedsRestart -or -not $apiOK) { Add-OptionalRow "api port $apiPort" $false ($apiDetail + '；插件可选择其他空闲端口。') }
else { Add-Row "api port $apiPort" $apiOK $apiDetail }

$rows | Format-Table -AutoSize
$failed = @($rows | Where-Object status -eq "FAIL")
if ($failed.Count -eq 0) {
  Write-Host "PREFLIGHT PASS"
  exit 0
}
Write-Host "PREFLIGHT FAILED: $($failed.Count) 项"
exit 1
