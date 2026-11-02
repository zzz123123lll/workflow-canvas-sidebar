param(
  [string]$Plugin = $PSScriptRoot,
  [string]$ProfilePath = "$env:USERPROFILE\.dsh\profiles\desktop"
)
$ErrorActionPreference = 'Stop'
$pluginRoot = (Resolve-Path -LiteralPath $Plugin).Path
$profileRoot = (Resolve-Path -LiteralPath $ProfilePath).Path
$packagePath = Join-Path $profileRoot 'package.json'
if (-not (Test-Path -LiteralPath $packagePath)) { throw '请先启动 Harness 创建 desktop profile，再登记本地插件。' }
$manifest = Get-Content -LiteralPath (Join-Path $pluginRoot 'package.json') -Raw | ConvertFrom-Json -AsHashtable
if ($manifest.name -ne 'dsh-workflow-canvas-sidebar') { throw '这不是工作流画布插件目录。' }
$linkPath = Join-Path $profileRoot 'node_modules\dsh-workflow-canvas-sidebar'
if (Test-Path -LiteralPath $linkPath) {
  $existing = Get-Item -LiteralPath $linkPath
  if (-not $existing.Target -or [System.IO.Path]::GetFullPath([string]$existing.Target) -ne $pluginRoot) {
    throw "已有同名插件指向其他目录。请把升级文件覆盖到现有插件目录；未替换链接或其他插件：$linkPath"
  }
}
$profileManifest = Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json -AsHashtable
if (-not $profileManifest.dependencies) { $profileManifest.dependencies = @{} }
if (-not $profileManifest.dsh) { $profileManifest.dsh = @{} }
if (-not $profileManifest.dsh.profile) { $profileManifest.dsh.profile = @{} }
$profileManifest.dependencies['dsh-workflow-canvas-sidebar'] = 'link:' + $pluginRoot.Replace('\', '/')
$bundles = @($profileManifest.dsh.profile.bundles | Where-Object { $_ })
if ('dsh-workflow-canvas-sidebar' -notin $bundles) { $bundles += 'dsh-workflow-canvas-sidebar' }
$profileManifest.dsh.profile.bundles = $bundles
$backupPath = $packagePath + '.canvas-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff')
Copy-Item -LiteralPath $packagePath -Destination $backupPath
if (-not (Test-Path -LiteralPath $linkPath)) {
  New-Item -ItemType Directory -Path (Split-Path -Parent $linkPath) -Force | Out-Null
  New-Item -ItemType Junction -Path $linkPath -Target $pluginRoot | Out-Null
}
$temporaryPath = $packagePath + '.canvas-tmp'
[System.IO.File]::WriteAllText($temporaryPath, ($profileManifest | ConvertTo-Json -Depth 30), [System.Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporaryPath -Destination $packagePath -Force
Write-Host "Registered workflow canvas $($manifest.version). Other bundles and dependencies preserved."
Write-Host "Profile backup: $backupPath"
Write-Host 'Restart Harness, then open 画布 and check version/data home in the usage guide.'
