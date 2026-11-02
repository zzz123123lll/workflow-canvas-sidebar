param(
  [string]$Source = "D:\bili-studio",
  [string]$CanvasHome = "$env:USERPROFILE\.dsh-workflow-canvas",
  [switch]$Force
)

$ErrorActionPreference = "Stop"

function Count-Files($path) {
  if (-not (Test-Path $path)) { return 0 }
  return (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count
}

$Source = [System.IO.Path]::GetFullPath($Source)
$CanvasHome = [System.IO.Path]::GetFullPath($CanvasHome)
if ($Source.TrimEnd('\') -eq $CanvasHome.TrimEnd('\')) {
  throw "Source 和 CanvasHome 是同一个目录，迁移没有意义。"
}
if (-not (Test-Path (Join-Path $Source "kb\modules.json"))) {
  throw "源目录不像 Canvas 数据目录（缺 kb\modules.json）：$Source"
}
$lock = Join-Path $Source ".runs\.lock"
if ((Test-Path $lock) -and -not $Force) {
  throw "源目录还有运行锁 .runs\.lock；先结束运行，或加 -Force。"
}
if (Test-Path $CanvasHome) {
  $existing = Get-ChildItem $CanvasHome -ErrorAction SilentlyContinue
  if ($existing -and -not $Force) {
    throw "目标目录已存在且非空：$CanvasHome；加 -Force 会先备份再覆盖。"
  }
}

$report = [ordered]@{
  source = $Source
  destination = $CanvasHome
  started_at = (Get-Date).ToString("o")
  copied = [ordered]@{}
  skipped = @()
}

if (Test-Path $CanvasHome) {
  $backup = "$CanvasHome.bak-" + (Get-Date -Format "yyyyMMdd-HHmmss")
  Write-Host "备份已有目标到 $backup ..."
  Move-Item $CanvasHome $backup
  $report.backup = $backup
}
New-Item -ItemType Directory -Path $CanvasHome -Force | Out-Null

foreach ($name in @("kb", "episodes", "artifacts", ".runs")) {
  $from = Join-Path $Source $name
  $to = Join-Path $CanvasHome $name
  if (-not (Test-Path $from)) {
    $report.skipped += $name
    continue
  }
  Write-Host "复制 $name ..."
  Copy-Item $from $to -Recurse -Force
  $report.copied[$name] = Count-Files $to
}

# Never migrate an active lock.
$targetLock = Join-Path $CanvasHome ".runs\.lock"
if (Test-Path $targetLock) { Remove-Item $targetLock -Force }

$modules = Join-Path $CanvasHome "kb\modules.json"
$canvas = Join-Path $CanvasHome "kb\canvas-workflow.json"
if (-not (Test-Path $modules)) { throw "迁移后缺少 kb\modules.json" }
if (-not (Test-Path $canvas)) { throw "迁移后缺少 kb\canvas-workflow.json" }
$report.module_count = (Get-Content $modules -Raw -Encoding UTF8 | ConvertFrom-Json).modules.Count
$report.canvas_nodes = (Get-Content $canvas -Raw -Encoding UTF8 | ConvertFrom-Json).nodes.Count
$report.finished_at = (Get-Date).ToString("o")
$reportFile = Join-Path $CanvasHome "migration-report.json"
$report | ConvertTo-Json -Depth 5 | Set-Content $reportFile -Encoding UTF8

Write-Host ""
Write-Host "迁移完成。"
Write-Host "  modules: $($report.module_count)"
Write-Host "  canvas nodes: $($report.canvas_nodes)"
Write-Host "  报告: $reportFile"
if ($report.backup) { Write-Host "  原目标备份: $($report.backup)" }
Write-Host ""
Write-Host "如果安装版插件使用默认 home，直接重启 DSH 即可。"
Write-Host "如果使用自定义 home，请设置 CANVAS_HOME 或安装器的 canvas-home.json。"
