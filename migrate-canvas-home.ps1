param(
  [Parameter(Mandatory=$true)][string]$Source,
  [string]$CanvasHome = "$env:USERPROFILE\.dsh-workflow-canvas",
  [string]$Python = '',
  [switch]$Force
)
$ErrorActionPreference = 'Stop'
if ($Force) { throw '新版迁移不移动或覆盖已有目录，请选择空目标目录。' }
if (-not $Python) { $Python = Join-Path $PSScriptRoot 'runtime\python\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw '请使用完整发行包的内置Python，或显式传入-Python。' }
& $Python (Join-Path $PSScriptRoot 'scripts\migrate.py') --source $Source --destination $CanvasHome
if ($LASTEXITCODE -ne 0) { throw '迁移未完成；源目录保留，请按上方错误处理后重试。' }
Write-Host '迁移完成。更新安装配置中的CanvasHome后重开Harness；视频工具需要在新目录重新配置。'
