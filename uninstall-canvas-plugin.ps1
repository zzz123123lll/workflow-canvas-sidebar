param(
  [string]$Plugin = $PSScriptRoot,
  [string]$ProfilePath = "$env:USERPROFILE\.dsh\profiles\desktop",
  [string]$CanvasHome = '',
  [switch]$DeleteData,
  [switch]$ShowPlan
)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) { throw '请使用PowerShell 7运行此脚本。' }
$pluginRoot = (Resolve-Path -LiteralPath $Plugin).Path
$manifest = Get-Content -LiteralPath (Join-Path $pluginRoot 'package.json') -Raw | ConvertFrom-Json
if ($manifest.name -ne 'dsh-workflow-canvas-sidebar') { throw '不是工作流画布插件目录。' }
$localConfigPath = Join-Path $pluginRoot 'canvas-home.json'
if (-not $CanvasHome -and (Test-Path -LiteralPath $localConfigPath)) {
  $CanvasHome = (Get-Content -LiteralPath $localConfigPath -Raw | ConvertFrom-Json).home
}
if (-not $CanvasHome) { $CanvasHome = Join-Path $env:USERPROFILE '.dsh-workflow-canvas' }
$dataRoot = [IO.Path]::GetFullPath($CanvasHome).TrimEnd('\','/')
$backupPath = $null
if ($DeleteData -and (Test-Path -LiteralPath $dataRoot)) {
  $dataItem = Get-Item -LiteralPath $dataRoot -Force
  $ancestorPath = $dataRoot
  while ($ancestorPath) {
    $ancestorItem = Get-Item -LiteralPath $ancestorPath -Force
    if ($ancestorItem.LinkType -or ($ancestorItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw '数据目录的任一级路径不能经过链接；未移动任何内容。'
    }
    $ancestorInfo = [IO.Directory]::GetParent($ancestorPath)
    $ancestorPath = if ($ancestorInfo) { $ancestorInfo.FullName } else { $null }
  }
  $resolvedRoot = (Resolve-Path -LiteralPath $dataRoot).Path.TrimEnd('\','/')
  $userRoot = [IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\','/')
  if ($dataItem.LinkType -or ($dataItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
      $resolvedRoot -ne $dataRoot -or $dataRoot -eq [IO.Path]::GetPathRoot($dataRoot).TrimEnd('\') -or
      $dataRoot -eq $userRoot -or $dataRoot -match '(?i)(^|[\\/])\.dsh([\\/]|$)') {
    throw '拒绝移动根目录、用户目录、宿主数据目录或链接。'
  }
  $markerPath = Join-Path $dataRoot '.workflow-canvas-home.json'
  if (-not (Test-Path -LiteralPath $markerPath)) { throw '目录没有插件归属标记；数据保留，请手动备份需要的文件。' }
  $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
  if ($marker.product -ne $manifest.name -or -not $marker.exclusive -or
      [IO.Path]::GetFullPath([string]$marker.data_home).TrimEnd('\','/') -ne $dataRoot) {
    throw '目录不是安装程序创建的独占插件数据目录；未移动任何内容。'
  }
  $allowed = @('kb','episodes','artifacts','.runs','skills','venv','bin','logs','.workflow-canvas-home.json')
  $unexpected = @(Get-ChildItem -LiteralPath $dataRoot -Force | Where-Object { $_.Name -notin $allowed })
  if ($unexpected.Count) { throw '目录存在非插件顶层内容；未移动数据，请先手动备份。' }
  $backupPath = $dataRoot + '.bak-uninstall-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
}
$profileRoot = [IO.Path]::GetFullPath($ProfilePath)
$packagePath = Join-Path $profileRoot 'package.json'
$profileManifest = $null
if (Test-Path -LiteralPath $packagePath) { $profileManifest = Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json -AsHashtable }
$linkPath = Join-Path $profileRoot 'node_modules\dsh-workflow-canvas-sidebar'
if (Test-Path -LiteralPath $linkPath) {
  $link = Get-Item -LiteralPath $linkPath -Force
  if (-not $link.LinkType -or [IO.Path]::GetFullPath([string]$link.Target) -ne $pluginRoot) {
    throw '同名插件登记不指向当前插件；未更改其他安装。'
  }
}
Write-Host "解除插件登记：$pluginRoot"
Write-Host $(if ($backupPath) { "数据仅移至备份：$backupPath" } else { "保留数据：$dataRoot" })
if ($ShowPlan) { Write-Host '仅展示计划，未修改。'; exit 0 }
if ($profileManifest) {
  Copy-Item -LiteralPath $packagePath -Destination ($packagePath + '.canvas-uninstall-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
  if ($profileManifest.dependencies) { $profileManifest.dependencies.Remove('dsh-workflow-canvas-sidebar') | Out-Null }
  if ($profileManifest.dsh -and $profileManifest.dsh.profile) {
    $profileManifest.dsh.profile.bundles = @($profileManifest.dsh.profile.bundles | Where-Object { $_ -ne 'dsh-workflow-canvas-sidebar' })
  }
  [IO.File]::WriteAllText($packagePath, ($profileManifest | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
}
if (Test-Path -LiteralPath $linkPath) { Remove-Item -LiteralPath $linkPath -Force }
if ($backupPath) { Move-Item -LiteralPath $dataRoot -Destination $backupPath }
Write-Host '卸载登记完成。代码与宿主账号/会话保留；重启Harness后生效。'
